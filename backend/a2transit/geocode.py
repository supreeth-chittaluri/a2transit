"""Address to coordinates, on somebody else's free service.

Two providers, both free and keyless, tried in order:

  * **Photon** (photon.komoot.io) — Komoot's OSM geocoder. No key, no account,
    no published rate limit beyond "be reasonable". Takes a `lat`/`lon` bias,
    which matters: "Main Street" is ambiguous everywhere and unambiguous within
    five miles of Ann Arbor.
  * **Nominatim** (OSM's own) — the fallback. Its usage policy is explicit: at
    most one request per second, and a User-Agent identifying the application.
    Both are honoured here. It is second rather than first precisely because
    that policy makes it the more precious resource.

Neither is a hard dependency of routing. A caller that already has coordinates —
the map's click handler, a saved place — never comes through here at all.

Results are bounded to the Ann Arbor / Ypsilanti area. A geocoder given
"Washington" will happily answer with the state, and a journey planner that
accepts that answer then reports that Seattle is unreachable by bus, which is
true and useless.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import httpx

from a2transit.routing.places import Place

logger = logging.getLogger(__name__)

#: Roughly Washtenaw County: everything either agency serves, and some margin.
#: (min_lon, min_lat, max_lon, max_lat)
SERVICE_AREA = (-84.10, 42.10, -83.55, 42.45)

#: Bias searches towards downtown Ann Arbor.
BIAS_LAT, BIAS_LON = 42.2808, -83.7430

PHOTON_URL = "https://photon.komoot.io/api"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

#: Required by Nominatim's usage policy, which rejects generic agents.
USER_AGENT = "a2transit/0.1 (https://github.com/a2transit; journey planner)"

#: Nominatim's policy cap, enforced process-wide rather than per-caller.
_NOMINATIM_MIN_INTERVAL = 1.0
_nominatim_lock = threading.Lock()
_nominatim_last_call = 0.0


class GeocodingError(RuntimeError):
    """No usable result, from any provider."""


@dataclass(frozen=True, slots=True)
class GeocodeResult:
    place: Place
    provider: str


def in_service_area(lat: float, lon: float) -> bool:
    min_lon, min_lat, max_lon, max_lat = SERVICE_AREA
    return min_lat <= lat <= max_lat and min_lon <= lon <= max_lon


def _photon(query: str, client: httpx.Client) -> Place | None:
    response = client.get(
        PHOTON_URL,
        params={"q": query, "lat": BIAS_LAT, "lon": BIAS_LON, "limit": 5},
        headers={"User-Agent": USER_AGENT},
    )
    response.raise_for_status()
    for feature in response.json().get("features", []):
        lon, lat = feature["geometry"]["coordinates"]
        if not in_service_area(lat, lon):
            continue
        properties = feature.get("properties", {})
        name = ", ".join(
            part
            for part in (
                properties.get("name"),
                properties.get("street"),
                properties.get("city"),
            )
            if part
        )
        return Place(name=name or query, lat=lat, lon=lon)
    return None


def _nominatim(query: str, client: httpx.Client) -> Place | None:
    global _nominatim_last_call

    with _nominatim_lock:
        wait = _NOMINATIM_MIN_INTERVAL - (time.monotonic() - _nominatim_last_call)
        if wait > 0:
            time.sleep(wait)
        _nominatim_last_call = time.monotonic()

    min_lon, min_lat, max_lon, max_lat = SERVICE_AREA
    response = client.get(
        NOMINATIM_URL,
        params={
            "q": query,
            "format": "jsonv2",
            "limit": 5,
            "viewbox": f"{min_lon},{max_lat},{max_lon},{min_lat}",
            "bounded": 1,
        },
        headers={"User-Agent": USER_AGENT},
    )
    response.raise_for_status()
    for result in response.json():
        lat, lon = float(result["lat"]), float(result["lon"])
        if in_service_area(lat, lon):
            return Place(name=result.get("display_name", query), lat=lat, lon=lon)
    return None


def geocode(query: str, *, client: httpx.Client | None = None) -> GeocodeResult:
    """First usable answer inside the service area, or raise.

    A provider erroring and a provider returning nothing are treated the same
    way — fall through to the next one — because from the caller's side they
    are the same thing, and because a free service being briefly unavailable is
    a normal Tuesday rather than an exception worth propagating.
    """
    owned = client is None
    client = client or httpx.Client(timeout=10.0, follow_redirects=True)
    try:
        for provider, lookup in (("photon", _photon), ("nominatim", _nominatim)):
            try:
                place = lookup(query, client)
            except httpx.HTTPError as exc:
                logger.warning("%s: %s", provider, exc)
                continue
            if place is not None:
                return GeocodeResult(place=place, provider=provider)
    finally:
        if owned:
            client.close()

    raise GeocodingError(
        f"no result for {query!r} inside the Ann Arbor service area — "
        "try adding the city, or give coordinates with --from-latlon"
    )


def parse_latlon(text: str, *, name: str | None = None) -> Place:
    """`42.2808,-83.7430` to a Place, for when the coordinates are already known."""
    try:
        lat_text, lon_text = text.split(",")
        lat, lon = float(lat_text), float(lon_text)
    except ValueError:
        raise GeocodingError(f"expected 'lat,lon', got {text!r}") from None
    if not in_service_area(lat, lon):
        raise GeocodingError(f"{lat},{lon} is outside the Ann Arbor service area")
    return Place(name=name or f"{lat:.5f},{lon:.5f}", lat=lat, lon=lon)


__all__ = [
    "SERVICE_AREA",
    "GeocodeResult",
    "GeocodingError",
    "geocode",
    "in_service_area",
    "parse_latlon",
]
