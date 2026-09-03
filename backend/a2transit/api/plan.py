"""`GET /plan` — the endpoint the whole project exists to serve.

An endpoint is `agency:stop_id` or `lat,lon`, and the two mix freely: a rider
standing at a map pin going to a named stop is an ordinary request. Coordinates
become a synthetic stop joined to the network by footpaths (see routing.places),
so the engines see one shape of query regardless.

The response is the whole Pareto set, not one answer. "Fastest" and "fewest
changes" are different journeys often enough to be worth showing both, and the
set is already computed — returning one would be throwing away the second
criterion RAPTOR gives for free.
"""

from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import Engine, text

from a2transit.api.schemas import PlanResponse, StopRef, to_itinerary_model
from a2transit.db.models import AgencySource
from a2transit.db.session import get_engine
from a2transit.routing.places import (
    ACCESS_MAX_METRES,
    Place,
    PlaceAttachment,
    attach_places,
)
from a2transit.routing.service import PlanRequest, itinerary_geometries, run_plan
from a2transit.routing.timetable import StopKey

logger = logging.getLogger(__name__)

router = APIRouter(tags=["planning"])

ATTRIBUTION = (
    "Transit scheduling, geographic, and real-time data provided by "
    "permission of AAATA/TheRide. Campus transit data from University of "
    "Michigan Transit Services."
)


def _parse_endpoint(value: str, label: str) -> StopKey | Place:
    """`theride:1338` or `42.2808,-83.7430`.

    Told apart by the separator rather than by trying one and catching the
    failure: a stop id could in principle contain a comma, and a silent
    misinterpretation here plans a journey from the wrong continent.
    """
    text_value = value.strip()
    if "," in text_value:
        try:
            lat_text, lon_text = text_value.split(",", 1)
            return Place(name=text_value, lat=float(lat_text), lon=float(lon_text))
        except ValueError:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"{label} looks like coordinates but is not 'lat,lon': {value!r}",
            ) from None

    if ":" not in text_value:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"{label} must be 'agency:stop_id' or 'lat,lon', got {value!r}. "
            "A bare stop id is ambiguous: 90 of them exist in both feeds.",
        )
    agency_name, stop_id = text_value.split(":", 1)
    try:
        agency = AgencySource(agency_name)
    except ValueError:
        valid = ", ".join(source.value for source in AgencySource)
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"unknown agency {agency_name!r} in {label} (expected one of {valid})",
        ) from None
    return (agency, stop_id)


def _stop_ref(engine: Engine, key: StopKey) -> StopRef:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT stop_name, stop_lat, stop_lon FROM stops "
                "WHERE agency_source = :agency AND stop_id = :stop_id"
            ),
            {"agency": key[0].value, "stop_id": key[1]},
        ).one_or_none()
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"no stop {key[0].value}:{key[1]}"
        )
    return StopRef(
        id=f"{key[0].value}:{key[1]}",
        name=row.stop_name,
        lat=row.stop_lat,
        lon=row.stop_lon,
        agency=key[0].value,
    )


def _route_colors(engine: Engine) -> dict[tuple[str, str], str | None]:
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT agency_source, route_id, route_color FROM routes")
        ).all()
    return {(row.agency_source, row.route_id): row.route_color for row in rows}


@router.get("/plan", response_model=PlanResponse, summary="Plan a journey")
def plan(
    origin: str = Query(
        ..., alias="from", description="`agency:stop_id` or `lat,lon`", examples=["theride:544"]
    ),
    destination: str = Query(
        ..., alias="to", description="`agency:stop_id` or `lat,lon`", examples=["mbus:207"]
    ),
    depart: dt.datetime | None = Query(
        None, description="ISO 8601 local time. Defaults to now."
    ),
    engine_name: str = Query("raptor", alias="engine", pattern="^(raptor|dijkstra)$"),
    geometry: bool = Query(True, description="Include route shapes for ride legs."),
) -> PlanResponse:
    engine = get_engine()
    departure = depart or dt.datetime.now().replace(microsecond=0)

    parsed_origin = _parse_endpoint(origin, "from")
    parsed_destination = _parse_endpoint(destination, "to")

    attachment: PlaceAttachment | None = None
    origin_ref: StopRef
    destination_ref: StopRef

    # A place at either end pulls both ends into the synthetic-stop world: the
    # attachment carries one origin and one destination, and mixing a real stop
    # key with a synthetic one on the same query would leave the real end with
    # no footpath to the other.
    if isinstance(parsed_origin, Place) or isinstance(parsed_destination, Place):
        origin_place = (
            parsed_origin
            if isinstance(parsed_origin, Place)
            else _place_from_stop(engine, parsed_origin)
        )
        destination_place = (
            parsed_destination
            if isinstance(parsed_destination, Place)
            else _place_from_stop(engine, parsed_destination)
        )
        attachment = attach_places(engine, origin_place, destination_place)
        if not attachment.is_routable:
            missing = "origin" if not attachment.origin_stops else "destination"
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"no stop within {int(ACCESS_MAX_METRES)} m of the {missing}",
            )
        origin_key, destination_key = attachment.origin, attachment.destination
        origin_ref = StopRef(
            id=None, name=origin_place.name, lat=origin_place.lat, lon=origin_place.lon
        )
        destination_ref = StopRef(
            id=None,
            name=destination_place.name,
            lat=destination_place.lat,
            lon=destination_place.lon,
        )
    else:
        origin_key, destination_key = parsed_origin, parsed_destination
        origin_ref = _stop_ref(engine, origin_key)
        destination_ref = _stop_ref(engine, destination_key)

    outcome = run_plan(
        engine,
        _cache(),
        PlanRequest(
            origin=origin_key,
            destination=destination_key,
            departure=departure,
            engine_name=engine_name,
            attachment=attachment,
        ),
    )

    colors = _route_colors(engine)
    return PlanResponse(
        origin=origin_ref,
        destination=destination_ref,
        requested_departure=departure,
        itineraries=[
            to_itinerary_model(
                itinerary,
                geometries=itinerary_geometries(engine, itinerary) if geometry else None,
                route_colors=colors,
            )
            for itinerary in outcome.itineraries
        ],
        engine=outcome.engine_name,
        query_ms=round(outcome.seconds * 1000, 3),
        attribution=ATTRIBUTION,
    )


def _place_from_stop(engine: Engine, key: StopKey) -> Place:
    """A real stop, treated as a place so it can be mixed with coordinates."""
    ref = _stop_ref(engine, key)
    return Place(name=ref.name, lat=ref.lat, lon=ref.lon)


def _cache():
    from a2transit.api.state import timetable_cache

    return timetable_cache()
