"""Routing from and to an arbitrary point, not just a stop.

A place — a street address, a map pin, the blue dot — is modelled as a stop that
no vehicle serves, joined to the real network by footpaths. That is the entire
mechanism. Nothing in either engine knows what a place is: RAPTOR walks out of
the origin in round 0 because it walks out of any origin, M2 gives the origin
node WALK edges because it gives every origin node WALK edges, and both count
walking into the destination as arriving because M4 made that true for stops.

The alternative was an access/egress layer in each engine — seed several
origins, take a minimum over several destinations — written twice, once per
engine, and correct in each only as long as someone kept them in step. The
whole point of the differential test is not to have code like that.

Two radii, because the two walks are not the same walk:

  * FOOTPATH_MAX_METRES (400 m) joins stop to stop mid-journey. A rider who has
    already paid for the trip will not go far to change vehicles.
  * ACCESS_MAX_METRES (800 m) joins a place to its stops. People walk further
    at the start and the end than they will in the middle, and a 400 m cap on
    the first leg makes a planner that shrugs at addresses a few streets from a
    route.

The candidate stops are capped as well as bounded. Downtown Ann Arbor has
around forty stops inside 800 m, and every one of them adds an edge to relax in
every round for a journey that will use one or two.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

from sqlalchemy import Engine, text

from a2transit.routing.constants import (
    FOOTPATH_MAX_METRES,
    effective_transfer_seconds,
    walking_seconds,
)
from a2transit.routing.patterns import RaptorTimetable
from a2transit.routing.timetable import Footpath, Stop, StopKey, Timetable


class PseudoAgency(StrEnum):
    """Not a feed. The leading key component for query-local synthetic stops.

    Every key in this project is `(agency, id)` because the two feeds collide on
    bare ids. A place belongs to neither feed, and giving it one of their names
    would put a row in the same namespace as real stops. `place` is a StrEnum
    member like AgencySource's, so it formats, compares and hashes identically —
    it simply never reaches the database.
    """

    PLACE = "place"


ORIGIN_KEY: StopKey = (PseudoAgency.PLACE, "origin")
DESTINATION_KEY: StopKey = (PseudoAgency.PLACE, "destination")

#: How far a rider will walk to reach the network, or to leave it.
ACCESS_MAX_METRES = 800.0

#: Most stops worth offering a place. Beyond this the extra candidates are
#: further away than ones already taken and cost a relaxation every round.
MAX_ACCESS_STOPS = 8

#: Beyond this, "just walk" stops being an answer and starts being a joke.
DIRECT_WALK_MAX_METRES = 2_000.0


@dataclass(frozen=True, slots=True)
class Place:
    """Somewhere a rider is, or wants to be."""

    name: str
    lat: float
    lon: float


@dataclass(frozen=True, slots=True)
class NearbyStop:
    stop: StopKey
    name: str
    metres: float

    @property
    def seconds(self) -> int:
        return effective_transfer_seconds(None, self.metres)


def nearby_stops(
    engine: Engine,
    place: Place,
    *,
    radius_metres: float = ACCESS_MAX_METRES,
    limit: int = MAX_ACCESS_STOPS,
) -> tuple[NearbyStop, ...]:
    """The stops a rider could reasonably walk to, nearest first.

    Ordered by distance and capped, so the result is deterministic — two
    equidistant stops would otherwise come back in whatever order the planner
    chose, and a seeded differential case would stop reproducing.
    """
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT s.agency_source, s.stop_id, s.stop_name,
                       ST_Distance(s.geog, ST_MakePoint(:lon, :lat)::geography) AS metres
                  FROM stops s
                 WHERE ST_DWithin(s.geog, ST_MakePoint(:lon, :lat)::geography, :radius)
                   -- Only stops something actually calls at. MBus ships two
                   -- "TEST STOP" rows, one of them 85 m from Michigan Stadium,
                   -- and offering it costs a real candidate its slot.
                   AND EXISTS (
                       SELECT 1 FROM stop_times st
                        WHERE st.agency_source = s.agency_source
                          AND st.stop_id = s.stop_id
                   )
                 ORDER BY metres, s.agency_source, s.stop_id
                 LIMIT :limit
                """
            ),
            {"lat": place.lat, "lon": place.lon, "radius": radius_metres, "limit": limit},
        ).all()

    return tuple(
        NearbyStop(
            stop=(row.agency_source, row.stop_id),
            name=row.stop_name,
            metres=row.metres,
        )
        for row in rows
    )


def _haversine_metres(a: Place, b: Place) -> float:
    """Straight-line distance, for the origin-to-destination walk only.

    Done in Python rather than in PostGIS because it is one pair of points and
    the round trip costs more than the arithmetic. Everything that has to agree
    with the footpath table still goes through PostGIS.
    """
    from math import asin, cos, radians, sin, sqrt

    earth_radius = 6_371_008.8
    lat1, lon1, lat2, lon2 = map(radians, (a.lat, a.lon, b.lat, b.lon))
    inner = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 2 * earth_radius * asin(sqrt(inner))


@dataclass(frozen=True, slots=True)
class PlaceAttachment:
    """Synthetic stops and the footpaths joining them to the real network."""

    origin: StopKey
    destination: StopKey
    stops: dict[StopKey, Stop]
    footpaths: tuple[Footpath, ...]
    origin_stops: tuple[NearbyStop, ...]
    destination_stops: tuple[NearbyStop, ...]
    direct_walk_metres: float | None

    @property
    def is_routable(self) -> bool:
        """False when a place has nothing to walk to and no direct walk either."""
        if self.direct_walk_metres is not None:
            return True
        return bool(self.origin_stops) and bool(self.destination_stops)


def _synthetic_stop(key: StopKey, place: Place) -> Stop:
    return Stop(
        key=key,
        stop_id=key[1],
        agency=key[0],  # type: ignore[arg-type]
        name=place.name,
        lat=place.lat,
        lon=place.lon,
    )


def attach_places(
    engine: Engine,
    origin: Place,
    destination: Place,
    *,
    radius_metres: float = ACCESS_MAX_METRES,
    limit: int = MAX_ACCESS_STOPS,
) -> PlaceAttachment:
    """Build the two synthetic stops and every footpath they need."""
    origin_stops = nearby_stops(engine, origin, radius_metres=radius_metres, limit=limit)
    destination_stops = nearby_stops(
        engine, destination, radius_metres=radius_metres, limit=limit
    )

    footpaths: list[Footpath] = []
    for nearby in origin_stops:
        footpaths.append(
            Footpath(ORIGIN_KEY, nearby.stop, nearby.seconds, None, nearby.metres)
        )
    for nearby in destination_stops:
        footpaths.append(
            Footpath(nearby.stop, DESTINATION_KEY, nearby.seconds, None, nearby.metres)
        )

    # Walking the whole way. Without this a three-block trip is answered with a
    # bus that arrives after the rider would have got there on foot, which is
    # the single most obvious way for a journey planner to look silly.
    direct = _haversine_metres(origin, destination)
    direct_metres = direct if direct <= DIRECT_WALK_MAX_METRES else None
    if direct_metres is not None:
        footpaths.append(
            Footpath(
                ORIGIN_KEY,
                DESTINATION_KEY,
                effective_transfer_seconds(None, direct_metres),
                None,
                direct_metres,
            )
        )

    return PlaceAttachment(
        origin=ORIGIN_KEY,
        destination=DESTINATION_KEY,
        stops={
            ORIGIN_KEY: _synthetic_stop(ORIGIN_KEY, origin),
            DESTINATION_KEY: _synthetic_stop(DESTINATION_KEY, destination),
        },
        footpaths=tuple(footpaths),
        origin_stops=origin_stops,
        destination_stops=destination_stops,
        direct_walk_metres=direct_metres,
    )


def with_places(timetable: Timetable, attachment: PlaceAttachment) -> Timetable:
    """A copy of the M2 timetable with the places attached.

    A copy, not a mutation: `compare_cases` builds one timetable per date and
    reuses it across every query on that date, so attaching in place would leave
    one rider's front door wired into the next rider's network.
    """
    return replace(
        timetable,
        stops={**timetable.stops, **attachment.stops},
        footpaths=timetable.footpaths + attachment.footpaths,
    )


def with_places_raptor(
    timetable: RaptorTimetable, attachment: PlaceAttachment
) -> RaptorTimetable:
    """The same, for RAPTOR's adjacency-shaped footpaths."""
    adjacency = dict(timetable.footpaths)
    for footpath in attachment.footpaths:
        adjacency[footpath.from_stop] = adjacency.get(footpath.from_stop, ()) + (
            (footpath.to_stop, footpath.seconds),
        )
    return replace(
        timetable,
        stops={**timetable.stops, **attachment.stops},
        footpaths=adjacency,
    )


__all__ = [
    "ACCESS_MAX_METRES",
    "DESTINATION_KEY",
    "DIRECT_WALK_MAX_METRES",
    "FOOTPATH_MAX_METRES",
    "MAX_ACCESS_STOPS",
    "ORIGIN_KEY",
    "NearbyStop",
    "Place",
    "PlaceAttachment",
    "PseudoAgency",
    "attach_places",
    "nearby_stops",
    "walking_seconds",
    "with_places",
    "with_places_raptor",
]
