"""In-memory timetable for a span of service dates.

Two things make this more than a query.

**The service-day window.** GTFS times count from service midnight and may pass
24:00:00, so a trip on service date D can arrive on calendar date D+1. 605 MBus
trips and 30 TheRide trips do exactly that, the latest reaching 27:15:00. A
query at 00:30 must therefore see trips belonging to service date D-1. The
timetable loads {D-1, D, D+1} and normalises every time to *seconds since
midnight of the query date*, so D-1 trips carry -86400 and D+1 trips +86400.
After that the search never thinks about dates again — it compares integers.

**A trip_id is not a journey.** The same trip_id runs on every date its service
is active, so the unit the search rides is a `TripInstance`: a trip bound to one
service date, and therefore to one time offset. Two instances of the same
trip_id on adjacent dates are genuinely different vehicles.

Underlying stop sequences are loaded once and shared between instances — a
weekday and the next weekday reference the same `Trip` object, not a copy.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field

from sqlalchemy import Engine, text

from a2transit.db.models import AgencySource
from a2transit.routing.constants import SECONDS_PER_DAY
from a2transit.routing.service_calendar import AgencyCalendar, load_calendars

logger = logging.getLogger(__name__)

#: (agency, stop_id) — never a bare stop_id. 90 stop_ids appear in both feeds.
StopKey = tuple[AgencySource, str]
#: (agency, trip_id) — 800 trip_ids appear in both feeds.
TripKey = tuple[AgencySource, str]

PICKUP_NONE = 1
DROP_OFF_NONE = 1


@dataclass(frozen=True, slots=True)
class Stop:
    key: StopKey
    stop_id: str
    agency: AgencySource
    name: str
    lat: float
    lon: float

    @property
    def label(self) -> str:
        return f"{self.name} ({self.agency.value}:{self.stop_id})"


@dataclass(frozen=True, slots=True)
class TripStop:
    """One row of stop_times, with GTFS-relative times."""

    stop: StopKey
    stop_sequence: int
    arrival: int
    departure: int
    #: TheRide marks 988 stop_times as no-pickup and 576 as no-drop-off —
    #: typically a terminal where riders may only get off, or only on.
    can_board: bool
    can_alight: bool


@dataclass(frozen=True, slots=True)
class Trip:
    key: TripKey
    trip_id: str
    agency: AgencySource
    route_id: str
    service_id: str
    headsign: str | None
    stops: tuple[TripStop, ...]


@dataclass(frozen=True, slots=True)
class TripInstance:
    """A trip bound to one service date, and so to one time offset."""

    trip: Trip
    service_date: dt.date
    offset: int

    @property
    def key(self) -> tuple[AgencySource, str, dt.date]:
        return (self.trip.agency, self.trip.trip_id, self.service_date)

    def departure_at(self, index: int) -> int:
        return self.trip.stops[index].departure + self.offset

    def arrival_at(self, index: int) -> int:
        return self.trip.stops[index].arrival + self.offset


@dataclass(frozen=True, slots=True)
class Footpath:
    """A walk between two stops, with the time it actually takes.

    `seconds` already carries the transfer floor and the detour allowance — see
    preprocess.footpaths — so an engine adds nothing to it.
    """

    from_stop: StopKey
    to_stop: StopKey
    seconds: int
    declared_seconds: int | None
    distance_metres: float

    @property
    def is_declared(self) -> bool:
        """Whether an agency publishes this pair in transfers.txt."""
        return self.declared_seconds is not None


@dataclass(frozen=True, slots=True)
class Route:
    key: tuple[AgencySource, str]
    short_name: str | None
    long_name: str | None
    color: str | None

    @property
    def label(self) -> str:
        return self.short_name or self.long_name or self.key[1]


@dataclass
class Timetable:
    """Everything the search needs for one query date, times already normalised."""

    base_date: dt.date
    stops: dict[StopKey, Stop]
    routes: dict[tuple[AgencySource, str], Route]
    instances: tuple[TripInstance, ...]
    footpaths: tuple[Footpath, ...] = field(default_factory=tuple)

    def stop_by_id(self, agency: AgencySource, stop_id: str) -> Stop | None:
        return self.stops.get((agency, stop_id))

    def find_stops_by_id(self, stop_id: str) -> tuple[Stop, ...]:
        """Every stop with this id, across agencies — usually one, sometimes two."""
        return tuple(stop for key, stop in self.stops.items() if key[1] == stop_id)

    def absolute_time(self, seconds: int) -> dt.datetime:
        """Seconds-since-base-midnight back to a real timestamp.

        Handles values below 0 and above 86400, which is the entire point of
        normalising rather than storing clock times.
        """
        return dt.datetime.combine(self.base_date, dt.time()) + dt.timedelta(seconds=seconds)

    def __repr__(self) -> str:
        return (
            f"<Timetable {self.base_date} stops={len(self.stops)} "
            f"instances={len(self.instances)} footpaths={len(self.footpaths)}>"
        )


def service_date_window(base_date: dt.date) -> tuple[tuple[dt.date, int], ...]:
    """The service dates a query on `base_date` must consider, with offsets.

    D-1 is included because its late trips run into D; D+1 because a query late
    on D can legitimately arrive after the following midnight.
    """
    return (
        (base_date - dt.timedelta(days=1), -SECONDS_PER_DAY),
        (base_date, 0),
        (base_date + dt.timedelta(days=1), SECONDS_PER_DAY),
    )


def load_stops(engine: Engine) -> dict[StopKey, Stop]:
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT agency_source, stop_id, stop_name, stop_lat, stop_lon FROM stops")
        ).all()
    return {
        (AgencySource(row.agency_source), row.stop_id): Stop(
            key=(AgencySource(row.agency_source), row.stop_id),
            stop_id=row.stop_id,
            agency=AgencySource(row.agency_source),
            name=row.stop_name,
            lat=row.stop_lat,
            lon=row.stop_lon,
        )
        for row in rows
    }


def load_routes(engine: Engine) -> dict[tuple[AgencySource, str], Route]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT agency_source, route_id, route_short_name, route_long_name, route_color "
                "FROM routes"
            )
        ).all()
    return {
        (AgencySource(row.agency_source), row.route_id): Route(
            key=(AgencySource(row.agency_source), row.route_id),
            short_name=row.route_short_name,
            long_name=row.route_long_name,
            color=row.route_color,
        )
        for row in rows
    }


def load_footpaths(engine: Engine) -> tuple[Footpath, ...]:
    """Every walkable stop-to-stop link, from the table preprocessing built.

    Both engines read this, which is the point: before M4 each derived its own
    transfer set from transfers.txt and closed it transitively, and the two
    only agreed because `constants.close_transfers` forced them to. Now they
    read the same rows, and "what is walkable" is a fact about the data rather
    than a convention two implementations have to keep in step.

    Ordered so the loaded set does not depend on the planner's row order — the
    engines are compared against each other case by case, and a build that
    shuffles is a mismatch nobody can reproduce.
    """
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT from_agency_source, from_stop_id,
                       to_agency_source, to_stop_id,
                       metres, seconds, declared_seconds
                  FROM footpaths
                 ORDER BY from_agency_source, from_stop_id,
                          to_agency_source, to_stop_id
                """
            )
        ).all()

    return tuple(
        Footpath(
            from_stop=(AgencySource(row.from_agency_source), row.from_stop_id),
            to_stop=(AgencySource(row.to_agency_source), row.to_stop_id),
            seconds=row.seconds,
            declared_seconds=row.declared_seconds,
            distance_metres=row.metres,
        )
        for row in rows
    )


def _load_trips(
    engine: Engine, wanted: dict[AgencySource, frozenset[str]]
) -> dict[TripKey, Trip]:
    """Load every trip whose service_id is active on any date in the window."""
    trips: dict[TripKey, Trip] = {}
    stops_by_trip: dict[TripKey, list[TripStop]] = {}

    with engine.connect() as connection:
        for agency, service_ids in wanted.items():
            if not service_ids:
                continue
            rows = connection.execute(
                text(
                    """
                    SELECT t.trip_id, t.route_id, t.service_id, t.trip_headsign,
                           st.stop_sequence, st.stop_id,
                           st.arrival_time, st.departure_time,
                           st.pickup_type, st.drop_off_type
                      FROM trips t
                      JOIN stop_times st
                        ON st.agency_source = t.agency_source AND st.trip_id = t.trip_id
                     WHERE t.agency_source = :agency
                       AND t.service_id = ANY(:services)
                     ORDER BY t.trip_id, st.stop_sequence
                    """
                ),
                {"agency": agency.value, "services": list(service_ids)},
            )

            headers: dict[TripKey, tuple[str, str, str | None]] = {}
            for row in rows:
                key: TripKey = (agency, row.trip_id)
                headers.setdefault(key, (row.route_id, row.service_id, row.trip_headsign))
                # A blank arrival or departure means an untimed stop. Neither
                # feed has any today; falling back to the other value keeps a
                # future one from crashing the search.
                arrival = row.arrival_time if row.arrival_time is not None else row.departure_time
                departure = (
                    row.departure_time if row.departure_time is not None else row.arrival_time
                )
                if arrival is None or departure is None:
                    continue
                stops_by_trip.setdefault(key, []).append(
                    TripStop(
                        stop=(agency, row.stop_id),
                        stop_sequence=row.stop_sequence,
                        arrival=arrival,
                        departure=departure,
                        can_board=row.pickup_type != PICKUP_NONE,
                        can_alight=row.drop_off_type != DROP_OFF_NONE,
                    )
                )

            for key, (route_id, service_id, headsign) in headers.items():
                sequence = stops_by_trip.get(key, [])
                # A one-stop trip cannot be ridden.
                if len(sequence) < 2:
                    continue
                trips[key] = Trip(
                    key=key,
                    trip_id=key[1],
                    agency=agency,
                    route_id=route_id,
                    service_id=service_id,
                    headsign=headsign,
                    stops=tuple(sequence),
                )

    return trips


def build_timetable(
    engine: Engine,
    base_date: dt.date,
    *,
    agencies: Sequence[AgencySource] | None = None,
    calendars: dict[AgencySource, AgencyCalendar] | None = None,
) -> Timetable:
    """Assemble the timetable for a query on `base_date`."""
    agencies = tuple(agencies) if agencies is not None else tuple(AgencySource)
    calendars = calendars if calendars is not None else load_calendars(engine, agencies)

    window = service_date_window(base_date)

    # Union of services needed anywhere in the window, so each trip's stop
    # sequence is fetched once even when several dates share a service.
    wanted: dict[AgencySource, frozenset[str]] = {}
    per_date: dict[tuple[AgencySource, dt.date], frozenset[str]] = {}
    for agency in agencies:
        calendar = calendars[agency]
        union: frozenset[str] = frozenset()
        for service_date, _ in window:
            active = calendar.active_on(service_date)
            per_date[(agency, service_date)] = active
            union |= active
        wanted[agency] = union

    trips = _load_trips(engine, wanted)

    instances: list[TripInstance] = []
    for agency in agencies:
        for service_date, offset in window:
            active = per_date[(agency, service_date)]
            for trip in trips.values():
                if trip.agency is agency and trip.service_id in active:
                    instances.append(TripInstance(trip, service_date, offset))

    timetable = Timetable(
        base_date=base_date,
        stops=load_stops(engine),
        routes=load_routes(engine),
        instances=tuple(instances),
        footpaths=load_footpaths(engine),
    )
    logger.info("built %r", timetable)
    return timetable


def iter_boardings(instances: Iterable[TripInstance]) -> Iterator[tuple[TripInstance, int]]:
    """Every (instance, index) a rider could board at, in no particular order."""
    for instance in instances:
        for index, trip_stop in enumerate(instance.trip.stops):
            # Never boardable at the final stop, whatever the feed says.
            if trip_stop.can_board and index < len(instance.trip.stops) - 1:
                yield instance, index
