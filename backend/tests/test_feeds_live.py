"""Live feed liveness checks.

Marked `network` and deselected by default (see pyproject.toml addopts), so the
default suite runs offline. Opt in with:

    pytest -m network

These guard the one failure mode no unit test can catch: an agency silently
moving or retiring an endpoint. Both of this project's static feed URLs replaced
URLs that had already gone dead, and TheRide's realtime endpoints are
undocumented, so this is a live risk rather than a theoretical one.
"""

from __future__ import annotations

import io
import zipfile

import httpx
import pytest
from google.transit import gtfs_realtime_pb2

from a2transit.config import get_settings

pytestmark = pytest.mark.network

settings = get_settings()

STATIC_FEEDS = [
    ("theride", settings.theride_gtfs_url),
    ("mbus", settings.mbus_gtfs_url),
]

REALTIME_FEEDS = [
    ("theride/vehicles", settings.theride_gtfsrt_vehicles_url),
    ("theride/trips", settings.theride_gtfsrt_trips_url),
    ("theride/alerts", settings.theride_gtfsrt_alerts_url),
    ("mbus/vehicles", settings.mbus_gtfsrt_vehicles_url),
    ("mbus/trips", settings.mbus_gtfsrt_trips_url),
    ("mbus/alerts", settings.mbus_gtfsrt_alerts_url),
]

REQUIRED_GTFS_FILES = {"agency.txt", "stops.txt", "routes.txt", "trips.txt", "stop_times.txt"}


@pytest.mark.parametrize(("name", "url"), STATIC_FEEDS, ids=[n for n, _ in STATIC_FEEDS])
def test_static_feed_is_a_zip_with_the_tables_ingest_needs(name: str, url: str) -> None:
    response = httpx.get(url, timeout=60.0, follow_redirects=True)
    response.raise_for_status()

    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert REQUIRED_GTFS_FILES <= set(archive.namelist())


@pytest.mark.parametrize(("name", "url"), REALTIME_FEEDS, ids=[n for n, _ in REALTIME_FEEDS])
def test_realtime_feed_parses_as_gtfs_realtime_v2(name: str, url: str) -> None:
    response = httpx.get(url, timeout=30.0, follow_redirects=True)
    response.raise_for_status()

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(response.content)

    assert feed.header.gtfs_realtime_version == "2.0"
    assert feed.header.timestamp > 0
