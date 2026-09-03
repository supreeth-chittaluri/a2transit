"""The layer between HTTP and the engines.

Two things live here that neither the engines nor the API should own.

**Timetables are cached by service date.** Building one costs ~330 ms for
RAPTOR and ~580 ms for the Dijkstra reference — fine for a CLI invocation,
absurd per request when the query itself takes 4 ms. They are pure functions of
the loaded feed, so caching them is safe until an ingest runs; `invalidate()`
is what a refresh calls. The cache is small on purpose: a rider planning
tomorrow's trip should not evict today's.

**Ride legs get their real geometry.** An itinerary drawn as straight lines
between stops is visibly wrong on a map — Ann Arbor's routes wind. GTFS ships
the shape, so each ride leg is clipped out of it with ST_LineSubstring between
the two stops' projections onto the line. Where a trip has no shape, or the
projection fails, the leg falls back to its stop coordinates: a slightly wrong
line is better than a missing one.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from sqlalchemy import Engine, text

from a2transit.routing.engine import PlanOutcome, plan_itineraries
from a2transit.routing.models import Itinerary, RideLeg
from a2transit.routing.patterns import RaptorTimetable, build_raptor_timetable
from a2transit.routing.places import PlaceAttachment, with_places, with_places_raptor
from a2transit.routing.timetable import StopKey, Timetable, build_timetable

logger = logging.getLogger(__name__)

#: Service dates held at once. Today, tomorrow, and whatever someone is poking
#: at — beyond that the memory is not worth the hit rate.
CACHE_SIZE = 4


class TimetableCache:
    """Date-keyed timetables, built once and shared across requests.

    Locked because FastAPI runs synchronous endpoints in a thread pool, so two
    requests for the same unseen date arrive concurrently on the first morning
    query of the day. The lock is held across the build, which makes the second
    request wait rather than duplicate 330 ms of work.
    """

    def __init__(self, engine: Engine, *, size: int = CACHE_SIZE) -> None:
        self._engine = engine
        self._size = size
        self._lock = threading.Lock()
        self._raptor: OrderedDict[dt.date, RaptorTimetable] = OrderedDict()
        self._dijkstra: OrderedDict[dt.date, Timetable] = OrderedDict()

    def raptor(self, day: dt.date) -> RaptorTimetable:
        with self._lock:
            if day not in self._raptor:
                started = time.perf_counter()
                self._raptor[day] = build_raptor_timetable(self._engine, day)
                logger.info(
                    "built RAPTOR timetable for %s in %.0f ms",
                    day,
                    (time.perf_counter() - started) * 1000,
                )
                self._trim(self._raptor)
            self._raptor.move_to_end(day)
            return self._raptor[day]

    def dijkstra(self, day: dt.date) -> Timetable:
        with self._lock:
            if day not in self._dijkstra:
                self._dijkstra[day] = build_timetable(self._engine, day)
                self._trim(self._dijkstra)
            self._dijkstra.move_to_end(day)
            return self._dijkstra[day]

    def _trim(self, cache: OrderedDict) -> None:
        while len(cache) > self._size:
            cache.popitem(last=False)

    def invalidate(self) -> None:
        """Drop everything. An ingest changes what the timetables are made of."""
        with self._lock:
            self._raptor.clear()
            self._dijkstra.clear()


@dataclass(frozen=True, slots=True)
class PlanRequest:
    origin: StopKey
    destination: StopKey
    departure: dt.datetime
    engine_name: str = "raptor"
    attachment: PlaceAttachment | None = None


def run_plan(engine: Engine, cache: TimetableCache, request: PlanRequest) -> PlanOutcome:
    day = request.departure.date()

    if request.engine_name == "dijkstra":
        timetable = cache.dijkstra(day)
        if request.attachment is not None:
            timetable = with_places(timetable, request.attachment)
        return plan_itineraries(
            engine,
            request.origin,
            request.destination,
            request.departure,
            engine_name="dijkstra",
            dijkstra_timetable=timetable,
        )

    timetable = cache.raptor(day)
    if request.attachment is not None:
        timetable = with_places_raptor(timetable, request.attachment)
    return plan_itineraries(
        engine,
        request.origin,
        request.destination,
        request.departure,
        engine_name="raptor",
        raptor_timetable=timetable,
    )


#: The stretch of a trip's published shape between two of its stops.
#:
#: ST_LineLocatePoint gives each stop's position along the line as a fraction,
#: and ST_LineSubstring cuts between them. LEAST/GREATEST because a stop pair
#: can project in either order on a shape that doubles back, and a substring
#: with its ends the wrong way round is empty rather than reversed.
_LEG_SHAPE_SQL = """
    WITH trip_shape AS (
        SELECT sg.geom
          FROM trips t
          JOIN shape_geometries sg
            ON sg.agency_source = t.agency_source AND sg.shape_id = t.shape_id
         WHERE t.agency_source = :agency AND t.trip_id = :trip_id
    ),
    ends AS (
        SELECT ts.geom,
               ST_LineLocatePoint(ts.geom, a.geog::geometry) AS from_fraction,
               ST_LineLocatePoint(ts.geom, b.geog::geometry) AS to_fraction
          FROM trip_shape ts
          JOIN stops a ON a.agency_source = :from_agency AND a.stop_id = :from_stop
          JOIN stops b ON b.agency_source = :to_agency   AND b.stop_id = :to_stop
    )
    SELECT ST_AsGeoJSON(
               ST_LineSubstring(
                   geom,
                   LEAST(from_fraction, to_fraction),
                   GREATEST(from_fraction, to_fraction)
               )
           ) AS geojson
      FROM ends
     WHERE from_fraction <> to_fraction
"""


def leg_geometry(engine: Engine, leg: RideLeg) -> list[list[float]] | None:
    """[[lon, lat], ...] along the route, or None to fall back to a straight line."""
    import json

    try:
        with engine.connect() as connection:
            geojson = connection.execute(
                text(_LEG_SHAPE_SQL),
                {
                    "agency": leg.agency.value,
                    "trip_id": leg.trip_id,
                    "from_agency": leg.from_stop.agency.value,
                    "from_stop": leg.from_stop.stop_id,
                    "to_agency": leg.to_stop.agency.value,
                    "to_stop": leg.to_stop.stop_id,
                },
            ).scalar()
    except Exception:
        logger.warning("shape lookup failed for %s %s", leg.agency, leg.trip_id, exc_info=True)
        return None

    if not geojson:
        return None
    coordinates = json.loads(geojson).get("coordinates")
    return coordinates if coordinates else None


def itinerary_geometries(
    engine: Engine, itinerary: Itinerary
) -> dict[int, list[list[float]]]:
    """Geometry per leg index, for the ride legs that have one."""
    geometries: dict[int, list[list[float]]] = {}
    for index, leg in enumerate(itinerary.legs):
        if not isinstance(leg, RideLeg):
            continue
        shape = leg_geometry(engine, leg)
        if shape:
            geometries[index] = shape
    return geometries


__all__ = [
    "CACHE_SIZE",
    "PlanRequest",
    "TimetableCache",
    "itinerary_geometries",
    "leg_geometry",
    "run_plan",
]
