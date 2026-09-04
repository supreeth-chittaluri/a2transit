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
* Walking into a stop is arriving there. Both engines were wrong about this
  through M3 — M2 targeted vehicle-arrival events, and RAPTOR copied it so the
  differential compared like with like — which meant a rider could be dropped
  200 m from where they were going and be told the trip was impossible. M4
  fixes it in both, together, because fixing it in one is a mismatch.
* One footpath per round, never two in a row. Walking is relaxed only out of
  stops a vehicle arrived at (and out of the origin, before round 1), so
  reaching a stop on foot does not license walking on from it. M2's graph
  enforces the same rule structurally, by only giving WALK edges to arrival
  nodes.

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
from a2transit.routing.graph import DEFAULT_HORIZON_SECONDS
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
    """Labels for every round, plus the parent pointers to rebuild journeys.

    There are two parent maps because there are two labels, and merging them is
    a real bug rather than a tidiness question. A stop can be *arrived at* by
    one vehicle and made *boardable* by a walk from somewhere else entirely, in
    the same round; one dictionary means whichever wrote last wins, and the
    journey rebuilt from the destination then ends at the wrong stop while
    still reporting the right time.

    That is not hypothetical. It was latent from M3 — with 15 declared
    transfers, all inside one transit centre, the two writes never landed on the
    same stop. With 8,308 footpaths they collide constantly: theride:544 ->
    theride:1019 at 09:00 came back claiming 09:17 at a stop two miles from the
    destination.
    """

    #: arrivals[k][stop] — earliest arrival using at most k vehicles, whether
    #: the last thing the rider did was ride or walk.
    arrivals: list[dict[StopKey, int]]
    #: ride_parents[k][stop] — the vehicle arrival, kept even when a walk beat
    #: it. Reconstruction needs the ride behind a walk, not the better label.
    ride_parents: list[dict[StopKey, RideStep]]
    #: walk_parents[k][stop] — the walk that arrived here, where one did.
    walk_parents: list[dict[StopKey, TransferStep]]
    #: ready_parents[k][stop] — how the rider came to be able to board here:
    #: the ride they got off, or the walk that brought them.
    ready_parents: list[dict[StopKey, Step]]
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
    """Walk parent pointers back from the destination label in round `rounds`.

    Which map to consult depends on what is being explained. The destination
    was arrived at, so it starts in `arrival_parents`; a ride's boarding stop is
    somewhere the rider was merely *standing*, so that is a `ready_parents`
    question, and the answer is a walk or is the alighting the previous round
    already accounts for.
    """
    steps: list[Step] = []
    stop = destination
    round_index = rounds

    # Did the rider walk the last stretch? Only if a walk is what produced the
    # label — a stop can have both a walk and a vehicle arrival in one round,
    # and following the wrong one gives a journey that does not add up.
    arrival = result.arrivals[round_index].get(stop)
    final_walk = result.walk_parents[round_index].get(stop)
    if final_walk is not None and final_walk.arrive == arrival:
        steps.append(final_walk)
        stop = final_walk.from_stop

    # Round 0 holds a whole journey when the destination was within walking
    # distance of the origin: no vehicle at all.
    if round_index == 0:
        return tuple(steps)

    while round_index > 0:
        ride = result.ride_parents[round_index].get(stop)
        if ride is None:
            # Nothing new happened at this stop in this round; the label came
            # from an earlier one, so drop back and keep walking.
            round_index -= 1
            continue

        steps.append(ride)
        stop = ride.board_stop
        round_index -= 1

        # A walk into the boarding stop belongs to the round that produced it.
        walk = result.ready_parents[round_index].get(stop)
        if isinstance(walk, TransferStep):
            steps.append(walk)
            stop = walk.from_stop

    steps.reverse()
    return tuple(steps)


def run_raptor(
    timetable: RaptorTimetable,
    origin: StopKey,
    departure_time: int,
    *,
    destination: StopKey | None = None,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    horizon_seconds: int = DEFAULT_HORIZON_SECONDS,
) -> RaptorResult:
    """Run the rounds. `destination` only enables pruning; labels are complete.

    `horizon_seconds` bounds boardings exactly as M2's graph does — no vehicle
    may be boarded after departure_time + horizon, though a ride boarded inside
    it keeps its whole stop sequence. Without this RAPTOR happily searches the
    entire three-day window and answers a query at 10:45 with a journey that
    boards at 18:24 and arrives the following morning: correct, useless, and
    not what M2 would have said.
    """
    boarding_deadline = departure_time + horizon_seconds
    arrivals: list[dict[StopKey, int]] = [{} for _ in range(max_rounds + 1)]
    ride_parents: list[dict[StopKey, RideStep]] = [{} for _ in range(max_rounds + 1)]
    walk_parents: list[dict[StopKey, TransferStep]] = [{} for _ in range(max_rounds + 1)]
    ready_parents: list[dict[StopKey, Step]] = [{} for _ in range(max_rounds + 1)]

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
    # journey through a footpath silently disappears.
    #
    # Each label carries its own parent map for the same reason: one vehicle can
    # arrive at a stop while a walk from somewhere else makes it boardable, and
    # a single map would lose whichever wrote first.
    ready: list[dict[StopKey, int]] = [{} for _ in range(max_rounds + 1)]

    # Two pruning bounds, because arriving by vehicle and arriving on foot are
    # not interchangeable: only a vehicle arrival licenses walking onward.
    #
    #   best_ride[stop]  earliest arrival *by vehicle*. Vehicle arrivals are
    #                    pruned against this and nothing else.
    #   best_any[stop]   earliest arrival by any means. What the destination is
    #                    measured on, and what bounds the search.
    #
    # Pruning a vehicle arrival against a foot arrival looks safe — it is
    # earlier, by every measure a rider cares about — and is not. theride:134
    # to theride:378 on a Saturday reached Green + Plymouth on foot at 15:51:21,
    # which suppressed the bus that got there at 15:52; the journey needed the
    # bus, because the last 380 m had to be walked and the rider had already
    # spent their one walk. RAPTOR returned nothing where M2 found a 45-minute
    # trip. Four of 500 differential cases, all on the same Saturday.
    best_ride: dict[StopKey, int] = {}
    best_any: dict[StopKey, int] = {}
    best_ready: dict[StopKey, int] = {origin: departure_time}

    arrivals[0][origin] = departure_time
    best_any[origin] = departure_time
    ready[0][origin] = departure_time
    marked: set[StopKey] = {origin}

    # Footpaths *out of the origin*, relaxed before round 1.
    #
    # Without this a rider starting at one Ypsilanti Transit Center bay cannot
    # walk to the next one and board there, because the in-round footpath pass
    # only relaxes stops that a vehicle arrived at. The origin never has a
    # vehicle arrival, so the walks out of it were silently unusable — one case
    # in 500 came back an hour late with three extra rides.
    #
    # No transfer floor is charged here: the rider is already standing at the
    # origin rather than alighting from something, which is how M2's graph
    # models it too.
    for target, seconds in timetable.footpaths.get(origin, ()):
        landed = departure_time + seconds
        step = TransferStep(
            from_stop=origin,
            to_stop=target,
            depart=departure_time,
            arrive=landed,
            seconds=seconds,
        )
        if landed < best_ready.get(target, INFINITY):
            best_ready[target] = landed
            ready[0][target] = landed
            ready_parents[0][target] = step
            marked.add(target)
        # Walking in is arriving, so a destination within walking distance of
        # the origin is answered in round 0, with no vehicle at all.
        if landed < best_any.get(target, INFINITY):
            best_any[target] = landed
            arrivals[0][target] = landed
            walk_parents[0][target] = step
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

        target_bound = best_any.get(destination, INFINITY) if destination else INFINITY
        previous_ready = ready[round_index - 1]

        # Vehicle arrivals made this round, which are the only stops a walk may
        # start from. Collected as the scan runs rather than read back out of
        # arrivals[], which also holds arrivals made on foot.
        rode_in: dict[StopKey, int] = {}

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
                    # Prune against this stop's best *vehicle* arrival and the
                    # destination's best of any kind: a label that cannot beat
                    # the destination cannot extend to anything that does.
                    if arrival < min(best_ride.get(stop, INFINITY), target_bound):
                        ride = RideStep(
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
                        best_ride[stop] = arrival
                        ride_parents[round_index][stop] = ride
                        rode_in[stop] = arrival
                        if arrival < best_any.get(stop, INFINITY):
                            best_any[stop] = arrival
                            arrivals[round_index][stop] = arrival
                        boardable = arrival + MIN_TRANSFER_SECONDS
                        if boardable < best_ready.get(stop, INFINITY):
                            ready[round_index][stop] = boardable
                            ready_parents[round_index][stop] = ride
                            best_ready[stop] = boardable
                        marked.add(stop)

                # Can the rider catch an earlier trip from here?
                if not pattern.can_board[position] or position == len(pattern.stops) - 1:
                    continue
                board_ready = previous_ready.get(stop)
                if board_ready is None or board_ready > boarding_deadline:
                    continue
                if run is not None and board_ready > run.departures[position]:
                    continue

                candidate = pattern.earliest_run(position, board_ready)
                if candidate is None:
                    continue
                # The horizon bounds the *departure*, not the rider's readiness.
                # Being ready at 09:00 does not license boarding the 19:00 bus:
                # M2's graph has no platform node out there, so it would find
                # nothing while RAPTOR happily returned a journey.
                if pattern.departure_columns[position][candidate] > boarding_deadline:
                    continue
                if run_index is None or candidate < run_index:
                    run_index = candidate
                    run = pattern.runs[candidate]
                    board_position = position

        # Footpaths settle inside the round that produced the arrival:
        # walking never adds a vehicle, so it must not consume a round.
        #
        # `rode_in` holds exactly the vehicle arrivals this round made, so a
        # stop a walk has already improved cannot become a walking source. The
        # rider would otherwise walk twice — 800 m, in one round, and only when
        # the iteration order happened to visit the two stops that way round. It
        # produced journeys whose legs did not join up: a 13-minute walk between
        # two stops with no footpath between them.
        for stop, arrival in rode_in.items():
            for target, seconds in timetable.footpaths.get(stop, ()):
                landed = arrival + MIN_TRANSFER_SECONDS + seconds
                step = TransferStep(
                    from_stop=stop,
                    to_stop=target,
                    depart=arrival,
                    arrive=landed,
                    seconds=MIN_TRANSFER_SECONDS + seconds,
                )
                if landed < best_ready.get(target, INFINITY):
                    best_ready[target] = landed
                    ready[round_index][target] = landed
                    ready_parents[round_index][target] = step
                    marked.add(target)
                # Walking in is arriving. Written under its own guard because
                # the two labels move independently: a stop can already have an
                # earlier vehicle arrival and still become boardable sooner on
                # foot, or the other way round.
                if landed < best_any.get(target, INFINITY):
                    best_any[target] = landed
                    arrivals[round_index][target] = landed
                    walk_parents[round_index][target] = step

        # A stop improved only as a boarding point carries no vehicle arrival,
        # so the next round must still see it as reachable. Its parent comes
        # along: a value inherited without one leaves the rebuilt journey with
        # no account of how the rider reached that stop.
        for stop, value in previous_ready.items():
            if value < ready[round_index].get(stop, INFINITY):
                ready[round_index][stop] = value
                inherited = ready_parents[round_index - 1].get(stop)
                if inherited is None:
                    ready_parents[round_index].pop(stop, None)
                else:
                    ready_parents[round_index][stop] = inherited

    return RaptorResult(
        arrivals=arrivals,
        ride_parents=ride_parents,
        walk_parents=walk_parents,
        ready_parents=ready_parents,
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

    # From round 0: a destination inside walking distance of the origin is
    # reached with no vehicle at all, and that is a legitimate answer rather
    # than the absence of one.
    for rounds in range(0, len(result.arrivals)):
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
