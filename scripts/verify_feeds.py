#!/usr/bin/env python3
"""Probe every agency feed and report whether it is still live and well-formed.

The feed URLs in docs/feeds.md were correct on 2026-09-03, but two of them had
already moved before this project started, and TheRide's realtime endpoints are
undocumented. Run this before blaming the routing engine for bad data:

    python scripts/verify_feeds.py

Exits non-zero if any feed is unreachable or unparseable, so it also works as a
CI canary. Needs `httpx` and `gtfs-realtime-bindings` (backend dev extras).
"""

from __future__ import annotations

import io
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
from google.transit import gtfs_realtime_pb2

TIMEOUT = 60.0

# Files a feed must contain for the M1 ingest to work at all.
REQUIRED_GTFS_FILES = {"agency.txt", "stops.txt", "routes.txt", "trips.txt", "stop_times.txt"}


@dataclass
class Agency:
    key: str
    label: str
    gtfs_url: str
    rt_urls: dict[str, str] = field(default_factory=dict)


AGENCIES = [
    Agency(
        key="theride",
        label="TheRide (AAATA)",
        gtfs_url="https://www.theride.org/sites/default/files/google/google_transit.zip",
        rt_urls={
            "vehicles": "https://rt.theride.org/gtfsrt/vehicles",
            "trips": "https://rt.theride.org/gtfsrt/trips",
            "alerts": "https://rt.theride.org/gtfsrt/alerts",
        },
    ),
    Agency(
        key="mbus",
        label="U-M MBus",
        gtfs_url="https://webapps.fo.umich.edu/transit_uploads/google_transit.zip",
        rt_urls={
            "vehicles": "https://mbus.ltp.umich.edu/gtfsrt/vehicles",
            "trips": "https://mbus.ltp.umich.edu/gtfsrt/trips",
            "alerts": "https://mbus.ltp.umich.edu/gtfsrt/alerts",
        },
    ),
]


def check_gtfs_static(client: httpx.Client, url: str) -> list[str]:
    """Download the ZIP and confirm it holds the GTFS tables the ingest needs."""
    problems: list[str] = []
    response = client.get(url, follow_redirects=True)
    response.raise_for_status()

    size_mb = len(response.content) / 1_048_576
    modified = response.headers.get("last-modified", "unknown")

    try:
        archive = zipfile.ZipFile(io.BytesIO(response.content))
    except zipfile.BadZipFile:
        return [f"response was not a valid ZIP ({size_mb:.2f} MB)"]

    names = set(archive.namelist())
    if missing := REQUIRED_GTFS_FILES - names:
        problems.append(f"missing required file(s): {', '.join(sorted(missing))}")

    counts = []
    for table in ("stops", "routes", "trips", "stop_times"):
        if f"{table}.txt" in names:
            with archive.open(f"{table}.txt") as handle:
                # Subtract the header row.
                counts.append(f"{table}={sum(1 for _ in handle) - 1:,}")

    print(f"    {size_mb:.2f} MB · modified {modified}")
    print(f"    {' · '.join(counts)}")
    return problems


def check_gtfs_realtime(client: httpx.Client, url: str) -> list[str]:
    """Fetch a GTFS-RT feed and confirm it parses as protobuf with fresh data."""
    response = client.get(url, follow_redirects=True)
    response.raise_for_status()

    feed = gtfs_realtime_pb2.FeedMessage()
    try:
        feed.ParseFromString(response.content)
    except Exception as exc:  # noqa: BLE001 — protobuf raises a bare DecodeError; a verifier should report any parse failure, not crash
        return [f"payload did not parse as GTFS-Realtime: {exc}"]

    header_time = datetime.fromtimestamp(feed.header.timestamp, tz=UTC)
    age = (datetime.now(tz=UTC) - header_time).total_seconds()
    print(
        f"    v{feed.header.gtfs_realtime_version} · {len(feed.entity)} entities"
        f" · header {age:.0f}s old"
    )

    problems: list[str] = []
    if not feed.entity:
        # Legitimate overnight when no buses run, so warn rather than fail.
        print("    NOTE: feed is empty — expected outside service hours")
    if age > 600:
        problems.append(f"header timestamp is stale ({age / 60:.0f} min old)")
    return problems


def main() -> int:
    failures: list[str] = []

    headers = {"User-Agent": "a2transit-feed-verifier/0.1"}
    with httpx.Client(timeout=TIMEOUT, headers=headers) as client:
        for agency in AGENCIES:
            print(f"\n{agency.label}")
            print(f"  GTFS static  {agency.gtfs_url}")
            try:
                for problem in check_gtfs_static(client, agency.gtfs_url):
                    failures.append(f"{agency.key} static: {problem}")
            except httpx.HTTPError as exc:
                failures.append(f"{agency.key} static: unreachable — {exc}")
                print(f"    UNREACHABLE: {exc}")

            for kind, url in agency.rt_urls.items():
                print(f"  GTFS-RT {kind:9} {url}")
                try:
                    for problem in check_gtfs_realtime(client, url):
                        failures.append(f"{agency.key} rt/{kind}: {problem}")
                except httpx.HTTPError as exc:
                    failures.append(f"{agency.key} rt/{kind}: unreachable — {exc}")
                    print(f"    UNREACHABLE: {exc}")

    print()
    if failures:
        print(f"{len(failures)} problem(s):")
        for failure in failures:
            print(f"  - {failure}")
        print("\nIf a URL moved, check mobilitydatabase.org and transit.land, then")
        print("update docs/feeds.md, .env.example, and this script together.")
        return 1

    print("All feeds live and well-formed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
