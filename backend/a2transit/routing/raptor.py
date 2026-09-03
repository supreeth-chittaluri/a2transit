"""RAPTOR — Round-Based Public Transit Routing.

Delling, Pajor, Werneck. Each round k extends journeys by exactly one more
vehicle, so after round k the labels hold the earliest arrival reachable using
at most k trips. That structure is what gives two criteria for the price of
one: earliest arrival is the best label over all rounds, and fewest transfers
falls out of *which* round first achieved a given arrival time.

Matching M2 exactly
-------------------
The differential test against M2's Dijkstra is only meaningful if both engines
model the same journey, so two of M2's conventions are reproduced here
deliberately rather than reinvented:

* Alighting costs MIN_TRANSFER_SECONDS; boarding is free. The rider's "ready to
  board" time at a stop is their arrival plus the floor, except at the origin,
  where they are already standing there.
* Arriving at the destination *on foot* does not count. M2's search targets
  vehicle-arrival events, so walking a declared transfer into the destination
  stop is not a completed journey there either. This is arguably wrong and both
  engines are wrong the same way; M4 makes footpaths first-class and is where it
  gets fixed.

Scanning a pattern
------------------
Within one round a pattern is traversed once, front to back, holding the trip
currently boarded. At each stop the rider may alight (improving that stop's
label) and may hop to an earlier trip if their previous-round arrival at this
stop allows catching one. Hopping only ever moves to an earlier trip, so each
pattern is scanned once per round regardless of how many trips it has.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from a2transit.routing.constants import MIN_TRANSFER_SECONDS
from a2transit.routing.patterns import PatternTable, RaptorTimetable, TripRun
from a2transit.routing.timetable import StopKey

logger = logging.getLogger(__name__)

INFINITY = 1 << 62

#: A journey needing more than this many vehicles is not one anybody would take.
#: Ann Arbor's whole network is 117 patterns; 6 is already generous.
DEFAULT_MAX_ROUNDS = 6


@dataclass(frozen=True, slots=True)
class RideStep:
    pattern_id: str
    route_id: str
    agency: object
    run: TripRun
    board_position: int
    alight_position: int
    board_stop: StopKey
    alight_stop: StopKey
    depart: int
    arrive: int


@dataclass(frozen=True, slots=True)
class TransferStep:
    from_stop: StopKey
    to_stop: StopKey
    depart: int
    arrive: int
    seconds: int


Step = RideStep | TransferStep


@dataclass(slots=True)
class RaptorResult:
    """Labels for every round, plus the parent pointers to rebuild journeys."""

    #: arrivals[k][stop] — earliest arrival by vehicle using at most k trips.
    arrivals: list[dict[StopKey, int]]
    #: parents[k][stop] — the step that produced arrivals[k][stop].
    parents: list[dict[StopKey, Step]]
    rounds_run: int
    patterns_scanned: int

    def best_arrival(self, stop: StopKey) -> int | None:
        best: int | None = None
        for round_labels in self.arrivals:
            value = round_labels.get(stop)
            if value is not None and (best is None or value < best):
                best = value
        return best

    def arrival_in_round(self, stop: StopKey, rounds: int) -> int | None:
        """Earliest arrival using at most `rounds` vehicles."""
        best: int | None = None
        for index in range(min(rounds, len(self.arrivals) - 1) + 1):
            value = self.arrivals[index].get(stop)
            if value is not None and (best is None or value < best):
                best = value
        return best


@dataclass(frozen=True, slots=True)
class ParetoEntry:
    """One non-dominated (arrival, vehicles) option."""

    rounds: int
    arrival: int
    steps: tuple[Step, ...]

    @property
    def transfers(self) -> int:
        return max(self.rounds - 1, 0)


def _reconstruct(result: RaptorResult, destination: StopKey, rounds: int) -> tuple[Step, ...]:
    """Walk parent pointers back from the destination label in round `rounds`."""
    steps: list[Step] = []
    stop = destination
    round_index = rounds

    while round_index > 0:
        step = result.parents[round_index].get(stop)
        if step is None:
            # Nothing new happened in this round; the label came from an
            # earlier one, so drop back and keep walking.
            round_index -= 1
            continue
        steps.append(step)
        if isinstance(step, RideStep):
            stop = step.board_stop
            round_index -= 1
        else:
            stop = step.from_stop
    steps.reverse()
    return tuple(steps)


def run_raptor(
    timetable: RaptorTimetable,
    origin: StopKey,
    departure_time: int,
    *,
    destination: StopKey | None = None,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
) -> RaptorResult:
    """Run the rounds. `destination` only enables pruning; labels are complete."""
    arrivals: list[dict[StopKey, int]] = [{} for _ in range(max_rounds + 1)]
    parents: list[dict[StopKey, Step]] = [{} for _ in range(max_rounds + 1)]

    # Two distinct labels per stop, and conflating them is a real bug:
    #
    #   arrivals[k][stop]  arrival *by vehicle*. This is what the destination
    #                      and the Pareto set are measured on, because M2 does
    #                      not treat walking into a stop as arriving there.
    #   ready[k][stop]     earliest moment the rider can board at that stop —
    #                      a vehicle arrival plus the transfer floor, or the far
    #                      end of a walk, or the query time at the origin.
    #
    # Only writing arrivals leaves a stop reached on foot unboardable, and every
    # journey through a declared transfer silently disappears.
    ready: list[dict[StopKey, int]] = [{} for _ in range(max_rounds + 1)]

    # Earliest known arrival at each stop across all rounds, used only to prune.
    best: dict[StopKey, int] = {}
    best_ready: dict[StopKey, int] = {origin: departure_time}

    arrivals[0][origin] = departure_time
    ready[0][origin] = departure_time
    marked: set[StopKey] = {origin}
    patterns_scanned = 0
    rounds_run = 0

    for round_index in range(1, max_rounds + 1):
        if not marked:
            break
        rounds_run = round_index

        # Collect the patterns worth scanning, each from the earliest position
        # any newly-improved stop sits at.
        queue: dict[int, int] = {}
        for stop in marked:
            for pattern_index, position in timetable.stop_index.get(stop, ()):
                current = queue.get(pattern_index)
                if current is None or position < current:
                    queue[pattern_index] = position
        marked = set()

        target_bound = best.get(destination, INFINITY) if destination else INFINITY
        previous_ready = ready[round_index - 1]

        for pattern_index, start_position in queue.items():
            pattern: PatternTable = timetable.patterns[pattern_index]
            patterns_scanned += 1

            run: TripRun | None = None
            run_index: int | None = None
            board_position = -1

            for position in range(start_position, len(pattern.stops)):
                stop = pattern.stops[position]

                if run is not None and pattern.can_alight[position]:
                    arrival = run.arrivals[position]
                    # Prune against both this stop's best and the destination's:
                    # a label that cannot beat the destination cannot extend to
                    # anything that does.
                    if arrival < min(best.get(stop, INFINITY), target_bound):
                        arrivals[round_index][stop] = arrival
                        best[stop] = arrival
                        boardable = arrival + MIN_TRANSFER_SECONDS
                        if boardable < best_ready.get(stop, INFINITY):
                            ready[round_index][stop] = boardable
                            best_ready[stop] = boardable
                        parents[round_index][stop] = RideStep(
                            pattern_id=pattern.pattern_id,
                            route_id=pattern.route_id,
                            agency=pattern.agency,
                            run=run,
                            board_position=board_position,
                            alight_position=position,
                            board_stop=pattern.stops[board_position],
                            alight_stop=stop,
                            depart=run.departures[board_position],
                            arrive=arrival,
                        )
                        marked.add(stop)

                # Can the rider catch an earlier trip from here?
                if not pattern.can_board[position] or position == len(pattern.stops) - 1:
                    continue
                board_ready = previous_ready.get(stop)
                if board_ready is None:
                    continue
                if run is not None and board_ready > run.departures[position]:
                    continue

                candidate = pattern.earliest_run(position, board_ready)
                if candidate is None:
                    continue
                if run_index is None or candidate < run_index:
                    run_index = candidate
                    run = pattern.runs[candidate]
                    board_position = position

        # Declared transfers settle inside the round that produced the arrival:
        # walking never adds a vehicle, so it must not consume a round.
        for stop in tuple(marked):
            arrival = arrivals[round_index].get(stop)
            if arrival is None:
                continue
            for target, seconds in timetable.transfers.get(stop, ()):
                landed = arrival + MIN_TRANSFER_SECONDS + seconds
                if landed < best_ready.get(target, INFINITY):
                    best_ready[target] = landed
                    ready[round_index][target] = landed
                    # Deliberately not written into arrivals[]: M2 does not
                    # treat walking into a stop as arriving there, and the two
                    # engines must agree for the differential test to mean
                    # anything. Fixed properly in M4.
                    parents[round_index][target] = TransferStep(
                        from_stop=stop,
                        to_stop=target,
                        depart=arrival,
                        arrive=landed,
                        seconds=MIN_TRANSFER_SECONDS + seconds,
                    )
                    marked.add(target)

        # A stop improved only as a boarding point carries no vehicle arrival,
        # so the next round must still see it as reachable.
        for stop, value in ready[round_index].items():
            ready[round_index].setdefault(stop, value)
        for stop, value in previous_ready.items():
            if value < ready[round_index].get(stop, INFINITY):
                ready[round_index][stop] = value

    return RaptorResult(
        arrivals=arrivals,
        parents=parents,
        rounds_run=rounds_run,
        patterns_scanned=patterns_scanned,
    )


def pareto_set(
    result: RaptorResult, destination: StopKey
) -> tuple[ParetoEntry, ...]:
    """Non-dominated (vehicles, arrival) options, fewest vehicles first.

    An entry survives only if it arrives strictly earlier than every option
    using fewer vehicles. Riding an extra bus to arrive at the same time or
    later is never worth offering.
    """
    entries: list[ParetoEntry] = []
    best_so_far: int | None = None

    for rounds in range(1, len(result.arrivals)):
        arrival = result.arrivals[rounds].get(destination)
        if arrival is None:
            continue
        if best_so_far is not None and arrival >= best_so_far:
            continue
        best_so_far = arrival
        entries.append(
            ParetoEntry(
                rounds=rounds,
                arrival=arrival,
                steps=_reconstruct(result, destination, rounds),
            )
        )

    return tuple(entries)


def fewest_transfers_arriving_by(
    entries: tuple[ParetoEntry, ...], deadline: int
) -> ParetoEntry | None:
    """Of the options arriving by `deadline`, the one using fewest vehicles.

    A different question from "the earliest arrival", and the one a rider with
    an appointment actually asks. The Pareto set is ordered by increasing
    vehicles and strictly decreasing arrival, so the first entry that meets the
    deadline is the answer.
    """
    for entry in entries:
        if entry.arrival <= deadline:
            return entry
    return None


@dataclass(frozen=True, slots=True)
class RaptorStats:
    rounds: int
    patterns_scanned: int
    seconds: float = field(default=0.0)

    def __str__(self) -> str:
        return (
            f"{self.rounds} rounds, {self.patterns_scanned} pattern scans"
            f"{f', {self.seconds * 1000:.1f} ms' if self.seconds else ''}"
        )


def to_datetime(timetable: RaptorTimetable, seconds: int) -> dt.datetime:
    return timetable.absolute_time(seconds)
