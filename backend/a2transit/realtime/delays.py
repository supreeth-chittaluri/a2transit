"""Applying live predictions to the timetable RAPTOR scans.

Neither feed publishes `delay`; both publish absolute predicted times. So a
delay only exists relative to the schedule, and this is the only module that
holds both. What comes out is a new `RaptorTimetable` with the affected runs
shifted — the router itself learns nothing about realtime, which is the point:
the second criterion, the horizon, the Pareto set and the differential tests all
keep working unchanged on a timetable that happens to describe the buses that
are actually running.

Three decisions worth stating.

**Only the affected patterns are rebuilt.** A few hundred trips have live
predictions out of 11,201 runs, so copying the whole structure per poll would be
most of a second of pointless work. Patterns nothing touches are passed through
by reference.

**A prediction is matched to the run it is nearest in time.** A trip_id repeats
on every service date its calendar is active, and the timetable holds three of
them ({D-1, D, D+1}); the feeds send no `start_date`. Choosing the run whose
scheduled start is closest to the predicted one gets the right instance without
reasoning about which side of midnight anything is on — which is the case that
would otherwise be wrong, silently, once a night.

**A delay carries forward, and never backwards.** GTFS-RT sends updates for the
stops it knows about; the spec says a `stop_time_update` applies to every
following stop until the next one. Stops before the first prediction keep their
scheduled time: the bus has already been there, and a rider cannot board the
past.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, replace

from a2transit.db.models import AgencySource
from a2transit.realtime.feeds import TripPrediction, service_seconds
from a2transit.routing.patterns import PatternTable, RaptorTimetable, TripRun

logger = logging.getLogger(__name__)

#: Predictions further than this from any scheduled run are dropped rather than
#: forced onto the nearest one. Six hours is far larger than any real delay and
#: far smaller than the gap between service dates, so it separates "this bus is
#: very late" from "this is a different day's trip".
MAX_MATCH_DRIFT_SECONDS = 6 * 3600


@dataclass(frozen=True, slots=True)
class DelayReport:
    """What a poll's worth of predictions did to the timetable."""

    trips_matched: int
    trips_unmatched: int
    trips_canceled: int
    runs_adjusted: int
    patterns_rebuilt: int
    #: Largest positive shift applied, in seconds. Negative delays (running
    #: early) happen and are applied, but this reports the headline number.
    max_delay_seconds: int

    def __str__(self) -> str:
        return (
            f"{self.runs_adjusted} runs across {self.patterns_rebuilt} patterns "
            f"({self.trips_matched} matched, {self.trips_unmatched} unmatched, "
            f"{self.trips_canceled} cancelled), worst delay "
            f"{self.max_delay_seconds // 60} min"
        )


def _shift_for_position(
    pattern: PatternTable,
    run: TripRun,
    prediction: TripPrediction,
    base_date: dt.date,
) -> list[int]:
    """Per-position delay in seconds, carried forward from each prediction.

    Matched on stop_id rather than stop_sequence. `position` is an index into
    the pattern and GTFS's stop_sequence is only required to increase — it may
    skip values, and the two are not the same number. Both feeds send stop_id,
    so there is no reason to guess.
    """
    by_stop: dict[str, int] = {}
    for stop in prediction.stops:
        predicted = stop.best_time
        if stop.stop_id is None or predicted is None:
            continue
        # Last update for a stop wins; a trip that visits a stop twice is not
        # something either feed publishes today.
        by_stop[stop.stop_id] = service_seconds(predicted, base_date)

    shifts: list[int] = []
    carried = 0
    for position, stop_key in enumerate(pattern.stops):
        predicted = by_stop.get(stop_key[1])
        if predicted is not None:
            carried = predicted - run.arrivals[position]
        shifts.append(carried)
    return shifts


def _match_run(
    runs: list[tuple[int, int, TripRun]], prediction: TripPrediction, base_date: dt.date
) -> tuple[int, int, TripRun] | None:
    """The (pattern index, run index, run) a prediction is talking about."""
    first = prediction.first_time
    if first is None:
        return None
    target = service_seconds(first, base_date)

    best: tuple[int, int, TripRun] | None = None
    best_drift: int | None = None
    for candidate in runs:
        drift = abs(candidate[2].departures[0] - target)
        if best_drift is None or drift < best_drift:
            best, best_drift = candidate, drift

    if best_drift is None or best_drift > MAX_MATCH_DRIFT_SECONDS:
        return None
    return best


def apply_predictions(
    timetable: RaptorTimetable,
    predictions: tuple[TripPrediction, ...],
) -> tuple[RaptorTimetable, DelayReport]:
    """A copy of `timetable` with live predictions folded in.

    A copy, never a mutation: the schedule timetable is cached and shared across
    requests, and a realtime overlay that edited it in place would make every
    later query depend on when the last poll happened to land.
    """
    if not predictions:
        return timetable, DelayReport(0, 0, 0, 0, 0, 0)

    # Where every trip_id lives: (pattern index, run index, run).
    index: dict[tuple[AgencySource, str], list[tuple[int, int, TripRun]]] = {}
    for pattern_index, pattern in enumerate(timetable.patterns):
        for run_index, run in enumerate(pattern.runs):
            index.setdefault((run.agency, run.trip_id), []).append(
                (pattern_index, run_index, run)
            )

    # pattern index -> {run index -> replacement run}, or None to drop it.
    edits: dict[int, dict[int, TripRun | None]] = {}
    matched = unmatched = canceled = 0
    max_delay = 0

    for prediction in predictions:
        candidates = index.get(prediction.key)
        if not candidates:
            unmatched += 1
            continue
        found = _match_run(candidates, prediction, timetable.base_date)
        if found is None:
            unmatched += 1
            continue
        matched += 1
        pattern_index, run_index, run = found

        if prediction.canceled:
            canceled += 1
            edits.setdefault(pattern_index, {})[run_index] = None
            continue

        pattern = timetable.patterns[pattern_index]
        shifts = _shift_for_position(pattern, run, prediction, timetable.base_date)
        if not any(shifts):
            continue
        max_delay = max(max_delay, max(shifts))
        edits.setdefault(pattern_index, {})[run_index] = replace(
            run,
            arrivals=tuple(
                arrival + shift for arrival, shift in zip(run.arrivals, shifts, strict=True)
            ),
            departures=tuple(
                departure + shift
                for departure, shift in zip(run.departures, shifts, strict=True)
            ),
        )

    if not edits:
        return timetable, DelayReport(matched, unmatched, canceled, 0, 0, 0)

    patterns = list(timetable.patterns)
    runs_adjusted = 0
    for pattern_index, replacements in edits.items():
        pattern = patterns[pattern_index]
        rebuilt = [
            replacements.get(run_index, run) if run_index in replacements else run
            for run_index, run in enumerate(pattern.runs)
        ]
        rebuilt = [run for run in rebuilt if run is not None]
        runs_adjusted += len(replacements)
        if not rebuilt:
            # Every run cancelled. Keeping an empty pattern would leave the
            # stop index pointing at nothing to scan, which is harmless but
            # confusing; the columns below would also be empty tuples.
            patterns[pattern_index] = replace(
                pattern, runs=(), departure_columns=(), sorted_columns=True
            )
            continue

        # Delays reorder trips. The whole reason `earliest_run` may bisect is
        # that a pattern's departure column is non-decreasing, and a bus running
        # twenty minutes late is exactly what breaks that — so the order and the
        # sortedness flag are both recomputed rather than inherited.
        rebuilt.sort(key=lambda run: (run.departures[0], run.trip_id))
        columns = tuple(
            tuple(run.departures[position] for run in rebuilt)
            for position in range(len(pattern.stops))
        )
        patterns[pattern_index] = replace(
            pattern,
            runs=tuple(rebuilt),
            departure_columns=columns,
            sorted_columns=all(
                all(a <= b for a, b in zip(column, column[1:], strict=False))
                for column in columns
            ),
        )

    report = DelayReport(
        trips_matched=matched,
        trips_unmatched=unmatched,
        trips_canceled=canceled,
        runs_adjusted=runs_adjusted,
        patterns_rebuilt=len(edits),
        max_delay_seconds=max_delay,
    )
    logger.info("realtime: %s", report)
    return replace(timetable, patterns=tuple(patterns)), report


__all__ = ["MAX_MATCH_DRIFT_SECONDS", "DelayReport", "apply_predictions"]
