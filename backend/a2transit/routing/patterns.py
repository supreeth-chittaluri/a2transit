"""Runtime pattern tables — the structures RAPTOR actually scans.

Built from the preprocessed `route_patterns` / `pattern_stops` /
`trips_by_pattern` tables plus the same {D-1, D, D+1} service window the M2
timetable uses, so both engines see exactly the same set of vehicles.

The shape is chosen for the two operations RAPTOR performs:

  * "which patterns serve this stop, and where" — `stop_index`
  * "earliest trip on this pattern I can catch at position p at time t" —
    a binary search down one column of `departure_columns`

`departure_columns` is the transpose of the per-trip arrays. Keeping both costs
about twice the stop_times of the window (~30 MB) and turns the earliest-trip
lookup from a scan into a bisect, which is the operation in the inner loop.
"""

from __future__ import annotations

import datetime as dt
import logging
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import Engine, text

from a2transit.db.models import AgencySource
from a2transit.routing.service_calendar import AgencyCalendar, load_calendars
from a2transit.routing.timetable import (
    Route,
    Stop,
    StopKey,
    load_footpaths,
    load_routes,
    load_stops,
    service_date_window,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TripRun:
    """One trip on one service date: absolute times, offset already applied."""

    trip_id: str
    agency: AgencySource
    service_date: dt.date
    arrivals: tuple[int, ...]
    departures: tuple[int, ...]


@dataclass(slots=True)
class PatternTable:
    pattern_id: str
    agency: AgencySource
    route_id: str
    stops: tuple[StopKey, ...]
    can_board: tuple[bool, ...]
    can_alight: tuple[bool, ...]
    #: Sorted by departure from position 0.
    runs: tuple[TripRun, ...]
    #: departure_columns[position][run_index] — the transpose of runs.
    departure_columns: tuple[tuple[int, ...], ...]
    #: False when some column is not non-decreasing, so bisect is unsound here.
    sorted_columns: bool

    def earliest_run(self, position: int, ready_at: int) -> int | None:
        """Index of the first run departing `position` at or after `ready_at`.

        Binary search where the column is sorted, linear scan where it is not.
        One MBus pattern has trips genuinely overtaking within a service day, so
        the fallback is not hypothetical — and a bisect there would silently
        return a trip that is not the earliest.
        """
        column = self.departure_columns[position]
        if self.sorted_columns:
            index = bisect_left(column, ready_at)
            return index if index < len(column) else None

        best_index: int | None = None
        best_time: int | None = None
        for index, departure in enumerate(column):
            if departure >= ready_at and (best_time is None or departure < best_time):
                best_index, best_time = index, departure
        return best_index


@dataclass
class RaptorTimetable:
    base_date: dt.date
    patterns: tuple[PatternTable, ...]
    #: stop -> [(pattern index, position)], the reverse index each round starts from.
    stop_index: dict[StopKey, tuple[tuple[int, int], ...]]
    #: stop -> [(target stop, seconds)] for every walkable link out of it.
    #: Adjacency rather than a flat list: the transfer pass of each round asks
    #: "where can I walk to from here" once per improved stop.
    footpaths: dict[StopKey, tuple[tuple[StopKey, int], ...]]
    #: Stop and route metadata, so a journey can be rendered without the M2
    #: timetable also being loaded.
    stops: dict[StopKey, Stop] = field(default_factory=dict)
    routes: dict[tuple[AgencySource, str], Route] = field(default_factory=dict)

    def absolute_time(self, seconds: int) -> dt.datetime:
        return dt.datetime.combine(self.base_date, dt.time()) + dt.timedelta(seconds=seconds)

    @property
    def run_count(self) -> int:
        return sum(len(pattern.runs) for pattern in self.patterns)

    def __repr__(self) -> str:
        return (
            f"<RaptorTimetable {self.base_date} patterns={len(self.patterns)} "
            f"runs={self.run_count} stops={len(self.stop_index)}>"
        )


def _load_pattern_shapes(
    engine: Engine,
) -> dict[tuple[AgencySource, str], tuple[str, list[StopKey], list[bool], list[bool]]]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT p.agency_source, p.pattern_id, p.route_id,
                       ps.position, ps.stop_id, ps.can_board, ps.can_alight
                  FROM route_patterns p
                  JOIN pattern_stops ps
                    ON ps.agency_source = p.agency_source AND ps.pattern_id = p.pattern_id
                 ORDER BY p.agency_source, p.pattern_id, ps.position
                """
            )
        ).all()

    shapes: dict[tuple[AgencySource, str], tuple[str, list[StopKey], list[bool], list[bool]]] = {}
    for row in rows:
        agency = AgencySource(row.agency_source)
        key = (agency, row.pattern_id)
        if key not in shapes:
            shapes[key] = (row.route_id, [], [], [])
        _, stops, boardable, alightable = shapes[key]
        stops.append((agency, row.stop_id))
        boardable.append(row.can_board)
        alightable.append(row.can_alight)
    return shapes


def _load_trip_times(
    engine: Engine, wanted_services: dict[AgencySource, frozenset[str]]
) -> dict[tuple[AgencySource, str], list[tuple[str, str, tuple[int, ...], tuple[int, ...]]]]:
    """Per pattern: [(trip_id, service_id, arrivals, departures)] in GTFS time."""
    result: dict[
        tuple[AgencySource, str], list[tuple[str, str, tuple[int, ...], tuple[int, ...]]]
    ] = defaultdict(list)

    with engine.connect() as connection:
        for agency, services in wanted_services.items():
            if not services:
                continue
            rows = connection.execute(
                text(
                    """
                    SELECT tbp.pattern_id, tbp.trip_id, tbp.service_id,
                           ps.position, st.arrival_time, st.departure_time
                      FROM trips_by_pattern tbp
                      JOIN pattern_stops ps
                        ON ps.agency_source = tbp.agency_source
                       AND ps.pattern_id = tbp.pattern_id
                      JOIN stop_times st
                        ON st.agency_source = tbp.agency_source
                       AND st.trip_id = tbp.trip_id
                       AND st.stop_id = ps.stop_id
                     WHERE tbp.agency_source = :agency
                       AND tbp.service_id = ANY(:services)
                     -- trip_order ranks within (pattern, service_id), so it is
                     -- not unique across a pattern; service_id must be in the
                     -- ordering or two trips interleave and each ends up with a
                     -- fragment of the other's stop times.
                     ORDER BY tbp.pattern_id, tbp.service_id, tbp.trip_order, ps.position
                    """
                ),
                {"agency": agency.value, "services": list(services)},
            )

            # Accumulated without a closure: the obvious `def flush()` here
            # captures the loop variables by reference, so it would append the
            # last agency's rows under whatever `agency` happened to be at call
            # time. Ruff's B023 catches exactly this.
            current: tuple[str, str, str] | None = None
            arrivals: list[int] = []
            departures: list[int] = []

            for row in rows:
                key = (row.pattern_id, row.trip_id, row.service_id)
                if key != current:
                    if current is not None:
                        result[(agency, current[0])].append(
                            (current[1], current[2], tuple(arrivals), tuple(departures))
                        )
                    current = key
                    arrivals = []
                    departures = []
                arrivals.append(row.arrival_time)
                departures.append(row.departure_time)

            if current is not None:
                result[(agency, current[0])].append(
                    (current[1], current[2], tuple(arrivals), tuple(departures))
                )

    return result


def _footpath_adjacency(engine: Engine) -> dict[StopKey, tuple[tuple[StopKey, int], ...]]:
    """The shared footpath set, grouped by origin stop.

    Same rows M2 loads, reshaped for the lookup RAPTOR performs. Deriving both
    from `load_footpaths` rather than from two queries is what keeps the engines
    honestly comparable — a difference in shape is fine, a difference in content
    would make the differential test meaningless.
    """
    grouped: dict[StopKey, list[tuple[StopKey, int]]] = defaultdict(list)
    for footpath in load_footpaths(engine):
        grouped[footpath.from_stop].append((footpath.to_stop, footpath.seconds))
    return {stop: tuple(targets) for stop, targets in grouped.items()}


def _columns_are_sorted(columns: tuple[tuple[int, ...], ...]) -> bool:
    return all(
        all(earlier <= later for earlier, later in zip(column, column[1:], strict=False))
        for column in columns
    )


def build_raptor_timetable(
    engine: Engine,
    base_date: dt.date,
    *,
    calendars: dict[AgencySource, AgencyCalendar] | None = None,
) -> RaptorTimetable:
    """Assemble the pattern tables for a query on `base_date`."""
    agencies = tuple(AgencySource)
    calendars = calendars if calendars is not None else load_calendars(engine, agencies)
    window = service_date_window(base_date)

    per_date: dict[tuple[AgencySource, dt.date], frozenset[str]] = {}
    wanted: dict[AgencySource, frozenset[str]] = {}
    for agency in agencies:
        union: frozenset[str] = frozenset()
        for service_date, _ in window:
            active = calendars[agency].active_on(service_date)
            per_date[(agency, service_date)] = active
            union |= active
        wanted[agency] = union

    shapes = _load_pattern_shapes(engine)
    trip_times = _load_trip_times(engine, wanted)

    patterns: list[PatternTable] = []
    for (agency, pattern_id), (route_id, stops, boardable, alightable) in sorted(
        shapes.items(), key=lambda item: (item[0][0].value, item[0][1])
    ):
        runs: list[TripRun] = []
        for trip_id, service_id, arrivals, departures in trip_times.get((agency, pattern_id), ()):
            for service_date, offset in window:
                if service_id not in per_date[(agency, service_date)]:
                    continue
                runs.append(
                    TripRun(
                        trip_id=trip_id,
                        agency=agency,
                        service_date=service_date,
                        arrivals=tuple(time + offset for time in arrivals),
                        departures=tuple(time + offset for time in departures),
                    )
                )
        if not runs:
            continue

        runs.sort(key=lambda run: (run.departures[0], run.trip_id))
        columns = tuple(
            tuple(run.departures[position] for run in runs) for position in range(len(stops))
        )
        patterns.append(
            PatternTable(
                pattern_id=pattern_id,
                agency=agency,
                route_id=route_id,
                stops=tuple(stops),
                can_board=tuple(boardable),
                can_alight=tuple(alightable),
                runs=tuple(runs),
                departure_columns=columns,
                # Checked against the merged three-day arrays actually being
                # searched, not against the preprocessing flag: merging service
                # dates could in principle disorder a pattern that is fine on
                # each day alone.
                sorted_columns=_columns_are_sorted(columns),
            )
        )

    stop_index: dict[StopKey, list[tuple[int, int]]] = defaultdict(list)
    for index, pattern in enumerate(patterns):
        for position, stop in enumerate(pattern.stops):
            stop_index[stop].append((index, position))

    timetable = RaptorTimetable(
        base_date=base_date,
        patterns=tuple(patterns),
        stop_index={stop: tuple(entries) for stop, entries in stop_index.items()},
        footpaths=_footpath_adjacency(engine),
        stops=load_stops(engine),
        routes=load_routes(engine),
    )
    unsorted = [pattern.pattern_id for pattern in patterns if not pattern.sorted_columns]
    if unsorted:
        logger.debug("%d pattern(s) scanned rather than bisected: %s", len(unsorted), unsorted)
    logger.info("built %r", timetable)
    return timetable
