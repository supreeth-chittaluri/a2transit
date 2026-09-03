"""Derive RAPTOR patterns from the loaded GTFS tables.

RAPTOR's unit of work is a "route" in its own sense: a set of trips sharing one
ordered stop sequence, which it scans exactly once per round. GTFS routes are
too coarse — TheRide's route 4 is two patterns, one per direction — so the
patterns are derived here rather than reused.

The payoff is how small the result is. 42 GTFS routes with trips become 117
patterns over 12,667 trips, averaging 21 stops each: roughly 2,500 pattern-stop
visits per round, against the 116,000-node graph M2's Dijkstra searches. That
ratio is the whole performance argument for M3.

Rebuilt wholesale on every run, for the same reason ingest is delete-and-reload:
these tables are a pure function of the GTFS tables, and a stale pattern that no
longer matches its trips is worse than no pattern at all.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import Engine, text

from a2transit.db.models import AgencySource

logger = logging.getLogger(__name__)

PICKUP_NONE = 1
DROP_OFF_NONE = 1

#: Children before parents, for the rebuild.
_DELETE_ORDER = ("trips_by_pattern", "stop_patterns", "pattern_stops", "route_patterns")


@dataclass(frozen=True, slots=True)
class _PatternStop:
    stop_id: str
    can_board: bool
    can_alight: bool


@dataclass
class _Pattern:
    signature: str
    route_id: str
    direction_id: int | None
    stops: tuple[_PatternStop, ...]
    #: (trip_id, service_id, first_departure, last_arrival)
    trips: list[tuple[str, str, int, int]]


@dataclass(frozen=True)
class PatternBuildResult:
    agency_source: AgencySource
    pattern_count: int
    pattern_stop_count: int
    trip_count: int
    seconds: float
    #: Patterns whose per-position departure columns are not sorted, so the
    #: router must scan them rather than binary-search.
    overtaking_patterns: tuple[str, ...] = ()


def _signature(stops: tuple[_PatternStop, ...]) -> str:
    """Hash the stop sequence together with its boarding rules.

    Including the rules costs nothing today — no two trips in either feed share
    a stop sequence while differing on pickup or drop-off. If one ever does,
    they must become separate patterns rather than one of them silently
    inheriting the other's boarding restrictions.
    """
    payload = "|".join(
        f"{stop.stop_id}:{int(stop.can_board)}{int(stop.can_alight)}" for stop in stops
    )
    return hashlib.sha1(payload.encode()).hexdigest()  # noqa: S324 - not security


def _collect_patterns(engine: Engine, agency: AgencySource) -> dict[str, _Pattern]:
    """Group every trip of one agency by its ordered stop sequence."""
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT t.trip_id, t.route_id, t.direction_id, t.service_id,
                       st.stop_id, st.stop_sequence,
                       st.pickup_type, st.drop_off_type,
                       st.arrival_time, st.departure_time
                  FROM trips t
                  JOIN stop_times st
                    ON st.agency_source = t.agency_source AND st.trip_id = t.trip_id
                 WHERE t.agency_source = :agency
                 ORDER BY t.trip_id, st.stop_sequence
                """
            ),
            {"agency": agency.value},
        )

        current_trip: str | None = None
        stops: list[_PatternStop] = []
        times: list[tuple[int, int]] = []
        header: tuple[str, int | None, str] = ("", None, "")
        patterns: dict[str, _Pattern] = {}

        def flush() -> None:
            if current_trip is None or len(stops) < 2:
                return
            sequence = tuple(stops)
            signature = _signature(sequence)
            route_id, direction_id, service_id = header
            pattern = patterns.get(signature)
            if pattern is None:
                pattern = _Pattern(signature, route_id, direction_id, sequence, [])
                patterns[signature] = pattern
            pattern.trips.append((current_trip, service_id, times[0][1], times[-1][0]))

        for row in rows:
            if row.trip_id != current_trip:
                flush()
                current_trip = row.trip_id
                header = (row.route_id, row.direction_id, row.service_id)
                stops = []
                times = []
            stops.append(
                _PatternStop(
                    stop_id=row.stop_id,
                    can_board=row.pickup_type != PICKUP_NONE,
                    can_alight=row.drop_off_type != DROP_OFF_NONE,
                )
            )
            times.append((row.arrival_time, row.departure_time))
        flush()

    return patterns


def _pattern_id(agency: AgencySource, route_id: str, ordinal: int) -> str:
    """Readable and stable: `theride:4:0`, `mbus:NW:1`.

    Stable across rebuilds because the ordinal comes from sorting patterns by
    signature within a route, not from iteration order.
    """
    return f"{agency.value}:{route_id}:{ordinal}"


def build_patterns(engine: Engine, agency: AgencySource) -> PatternBuildResult:
    """Rebuild every pattern table for one agency, in a single transaction."""
    started = time.perf_counter()
    patterns = _collect_patterns(engine, agency)

    by_route: dict[str, list[_Pattern]] = defaultdict(list)
    for pattern in patterns.values():
        by_route[pattern.route_id].append(pattern)

    identified: list[tuple[str, _Pattern]] = []
    for route_id, group in sorted(by_route.items()):
        for ordinal, pattern in enumerate(sorted(group, key=lambda item: item.signature)):
            identified.append((_pattern_id(agency, route_id, ordinal), pattern))

    pattern_stop_count = 0
    trip_count = 0

    with engine.begin() as connection:
        for table in _DELETE_ORDER:
            connection.execute(
                text(f"DELETE FROM {table} WHERE agency_source = :agency"),
                {"agency": agency.value},
            )

        driver_connection = connection.connection.driver_connection

        with driver_connection.cursor() as cursor:
            with cursor.copy(
                "COPY route_patterns (agency_source, pattern_id, route_id, direction_id, "
                "signature, stop_count, trip_count, has_overtaking) FROM STDIN"
            ) as copy:
                for pattern_id, pattern in identified:
                    copy.write_row(
                        (
                            agency.value,
                            pattern_id,
                            pattern.route_id,
                            pattern.direction_id,
                            pattern.signature,
                            len(pattern.stops),
                            len(pattern.trips),
                            # Filled in below, once the trip ordering exists.
                            False,
                        )
                    )

            with cursor.copy(
                "COPY pattern_stops (agency_source, pattern_id, position, stop_id, "
                "can_board, can_alight) FROM STDIN"
            ) as copy:
                for pattern_id, pattern in identified:
                    for position, stop in enumerate(pattern.stops):
                        copy.write_row(
                            (
                                agency.value,
                                pattern_id,
                                position,
                                stop.stop_id,
                                stop.can_board,
                                stop.can_alight,
                            )
                        )
                        pattern_stop_count += 1

            with cursor.copy(
                "COPY stop_patterns (agency_source, stop_id, pattern_id, position) FROM STDIN"
            ) as copy:
                for pattern_id, pattern in identified:
                    for position, stop in enumerate(pattern.stops):
                        copy.write_row((agency.value, stop.stop_id, pattern_id, position))

            with cursor.copy(
                "COPY trips_by_pattern (agency_source, pattern_id, trip_id, service_id, "
                "trip_order, first_departure, last_arrival) FROM STDIN"
            ) as copy:
                for pattern_id, pattern in identified:
                    # Ranked within (pattern, service_id), not across the whole
                    # pattern. A global ordering interleaves trips from
                    # different service days, whose running times differ, and
                    # invents overtaking that happens on no real day — 12
                    # patterns look violating that way, 11 of them spuriously.
                    per_service: dict[str, list[tuple[str, str, int, int]]] = defaultdict(list)
                    for trip in pattern.trips:
                        per_service[trip[1]].append(trip)
                    for service_trips in per_service.values():
                        ordered = sorted(service_trips, key=lambda item: (item[2], item[0]))
                        for trip_order, (trip_id, service_id, first, last) in enumerate(ordered):
                            copy.write_row(
                                (
                                    agency.value,
                                    pattern_id,
                                    trip_id,
                                    service_id,
                                    trip_order,
                                    first,
                                    last,
                                )
                            )
                            trip_count += 1

        # The detection query joins the tables we have just COPY'd into. Without
        # statistics the planner has no idea of their size and picks a plan that
        # takes ~20 s instead of ~0.1 s.
        connection.execute(
            text("ANALYZE route_patterns, pattern_stops, stop_patterns, trips_by_pattern")
        )
        overtaking = _mark_overtaking(connection, agency)

    result = PatternBuildResult(
        agency_source=agency,
        overtaking_patterns=tuple(overtaking),
        pattern_count=len(identified),
        pattern_stop_count=pattern_stop_count,
        trip_count=trip_count,
        seconds=time.perf_counter() - started,
    )
    logger.info(
        "%s: %d patterns, %d pattern stops, %d trips in %.1fs",
        agency.value,
        result.pattern_count,
        result.pattern_stop_count,
        result.trip_count,
        result.seconds,
    )
    return result


_OVERTAKING_SQL = """
    -- Compare each trip against its immediate predecessor in departure order,
    -- *within one service day*. A self-join would materialise ~215k rows twice;
    -- LAG does it in one pass.
    WITH ordered AS (
        SELECT tbp.pattern_id, ps.position, st.arrival_time,
               lag(st.arrival_time) OVER (
                   PARTITION BY tbp.pattern_id, tbp.service_id, ps.position
                   ORDER BY tbp.trip_order
               ) AS previous_arrival
          FROM trips_by_pattern tbp
          JOIN pattern_stops ps
            ON ps.agency_source = tbp.agency_source
           AND ps.pattern_id = tbp.pattern_id
          JOIN stop_times st
            ON st.agency_source = tbp.agency_source
           AND st.trip_id = tbp.trip_id
           AND st.stop_id = ps.stop_id
         WHERE tbp.agency_source = :agency
    )
    SELECT DISTINCT pattern_id
      FROM ordered
     WHERE previous_arrival IS NOT NULL
       AND arrival_time < previous_arrival
"""


def find_overtaking_patterns(connection, agency: AgencySource) -> list[str]:
    """Patterns where a later-departing trip arrives somewhere earlier.

    RAPTOR's "earliest trip catchable at position i" is a binary search over one
    departure order per pattern, which is only sound when trips do not overtake.
    Where they do, the per-position departure column is unsorted and a binary
    search silently returns a trip that is not the earliest.

    Comparing within a service day matters. Ordering a pattern's trips globally
    mixes service days whose running times differ and manufactures violations
    that occur on no real day: 12 patterns look violating that way, and 11 of
    them are artefacts. One MBus pattern overtakes for real, by up to 60 s.
    """
    return list(
        connection.execute(text(_OVERTAKING_SQL), {"agency": agency.value}).scalars().all()
    )


def _mark_overtaking(connection, agency: AgencySource) -> list[str]:
    offenders = find_overtaking_patterns(connection, agency)
    if offenders:
        connection.execute(
            text(
                "UPDATE route_patterns SET has_overtaking = true "
                "WHERE agency_source = :agency AND pattern_id = ANY(:ids)"
            ),
            {"agency": agency.value, "ids": offenders},
        )
        logger.warning(
            "%s: %d pattern(s) have overtaking trips and will be scanned, not "
            "binary-searched: %s",
            agency.value,
            len(offenders),
            ", ".join(sorted(offenders)),
        )
    return offenders


def verify_no_overtaking(engine: Engine, agency: AgencySource) -> list[str]:
    """Standalone wrapper over find_overtaking_patterns, for tests and the CLI."""
    with engine.connect() as connection:
        return find_overtaking_patterns(connection, agency)
