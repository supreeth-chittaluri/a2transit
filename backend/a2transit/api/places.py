"""`GET /geocode` — address to coordinates, so the browser need not hold a key.

Proxied through the API rather than called from the frontend directly for two
reasons. Nominatim's usage policy asks for a identifying User-Agent and caps
requests to one a second, neither of which a browser tab can honestly promise;
and keeping it server-side means the frontend has one origin to talk to, so no
second CORS relationship to maintain with somebody else's free service.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status

from a2transit.api.schemas import GeocodeResponse
from a2transit.geocode import GeocodingError, geocode

router = APIRouter(tags=["places"])


@router.get("/geocode", response_model=GeocodeResponse, summary="Address to coordinates")
def geocode_address(
    query: str = Query(..., alias="q", min_length=2, examples=["Michigan Stadium"]),
) -> GeocodeResponse:
    try:
        result = geocode(query)
    except GeocodingError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None

    return GeocodeResponse(
        query=query,
        name=result.place.name,
        lat=result.place.lat,
        lon=result.place.lon,
        provider=result.provider,
    )
