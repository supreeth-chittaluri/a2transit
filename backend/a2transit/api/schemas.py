"""Response models for the HTTP API.

Field names are camelCase on the wire and snake_case in Python: the frontend is
TypeScript and should not have to think about it, and the backend should not
have to write `walkSeconds` in the middle of Python code. Pydantic's alias
generator does the translation in one place.

Stops are identified as `agency:stop_id` in a single string, not as two fields.
The composite key is the invariant the whole schema is built on — 90 stop_ids
appear in both feeds as different places — and splitting it into two fields
invites a caller to drop one.
"""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from a2transit.db.models import AgencySource
from a2transit.routing.models import Itinerary, LegKind, RideLeg
from a2transit.routing.timetable import Stop


class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class StopRef(ApiModel):
    #: `agency:stop_id`, or null for a place that is not a stop.
    id: str | None
    name: str
    lat: float
    lon: float
    agency: str | None = None

    @classmethod
    def of(cls, stop: Stop) -> StopRef:
        is_real = stop.agency in tuple(AgencySource)
        return cls(
            id=f"{stop.agency.value}:{stop.stop_id}" if is_real else None,
            name=stop.name,
            lat=stop.lat,
            lon=stop.lon,
            agency=stop.agency.value if is_real else None,
        )


class LegModel(ApiModel):
    kind: Literal["ride", "walk"]
    from_stop: StopRef
    to_stop: StopRef
    depart: dt.datetime
    arrive: dt.datetime
    seconds: int

    # Ride legs only.
    agency: str | None = None
    route_id: str | None = None
    route_label: str | None = None
    route_color: str | None = None
    trip_id: str | None = None
    headsign: str | None = None
    intermediate_stops: int | None = None
    #: [[lon, lat], ...] along the published shape. Absent for walks, and for a
    #: ride whose trip ships no shape.
    geometry: list[list[float]] | None = None

    # Walk legs only.
    distance_metres: float | None = None


class ItineraryModel(ApiModel):
    departure: dt.datetime
    arrival: dt.datetime
    #: Door to door, counting the wait before the first departure.
    duration_seconds: int
    transfers: int
    ride_count: int
    walk_seconds: int
    legs: list[LegModel]


class PlanResponse(ApiModel):
    origin: StopRef
    destination: StopRef
    requested_departure: dt.datetime
    #: Fewest vehicles first, strictly earlier arrivals after — the Pareto set.
    itineraries: list[ItineraryModel]
    engine: str
    query_ms: float
    attribution: str


class StopSearchResult(ApiModel):
    id: str
    name: str
    agency: str
    lat: float
    lon: float
    #: Routes calling here, for disambiguating three stops with the same name.
    routes: list[str]


class StopSearchResponse(ApiModel):
    query: str
    results: list[StopSearchResult]


class DepartureModel(ApiModel):
    agency: str
    route_id: str
    route_label: str
    route_color: str | None
    trip_id: str
    headsign: str | None
    departure: dt.datetime
    #: Seconds from the query time. Negative never appears; the board looks
    #: forward only.
    in_seconds: int


class DeparturesResponse(ApiModel):
    stop: StopRef
    at: dt.datetime
    departures: list[DepartureModel]


class GeocodeResponse(ApiModel):
    query: str
    name: str
    lat: float
    lon: float
    provider: str


def _walk_seconds(itinerary: Itinerary) -> int:
    return int(
        sum(
            leg.duration.total_seconds()
            for leg in itinerary.legs
            if leg.kind is LegKind.TRANSFER
        )
    )


def to_itinerary_model(
    itinerary: Itinerary,
    *,
    geometries: dict[int, list[list[float]]] | None = None,
    route_colors: dict[tuple[str, str], str | None] | None = None,
) -> ItineraryModel:
    geometries = geometries or {}
    route_colors = route_colors or {}
    legs: list[LegModel] = []

    for index, leg in enumerate(itinerary.legs):
        common = {
            "from_stop": StopRef.of(leg.from_stop),
            "to_stop": StopRef.of(leg.to_stop),
            "depart": leg.depart,
            "arrive": leg.arrive,
            "seconds": int(leg.duration.total_seconds()),
        }
        if isinstance(leg, RideLeg):
            color = route_colors.get((leg.agency.value, leg.route_id))
            legs.append(
                LegModel(
                    kind="ride",
                    agency=leg.agency.value,
                    route_id=leg.route_id,
                    route_label=leg.route_label,
                    route_color=f"#{color}" if color else None,
                    trip_id=leg.trip_id,
                    headsign=leg.headsign,
                    intermediate_stops=leg.intermediate_stops,
                    geometry=geometries.get(index),
                    **common,
                )
            )
        else:
            legs.append(
                LegModel(kind="walk", distance_metres=leg.distance_metres, **common)
            )

    return ItineraryModel(
        departure=itinerary.departure,
        arrival=itinerary.arrival,
        duration_seconds=int(itinerary.duration.total_seconds()),
        transfers=itinerary.transfer_count,
        ride_count=len(itinerary.ride_legs),
        walk_seconds=_walk_seconds(itinerary),
        legs=legs,
    )
