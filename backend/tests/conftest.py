from __future__ import annotations

import csv
import io
import zipfile
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

import pytest
import sqlalchemy
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text

from a2transit.config import get_settings
from a2transit.db import schema
from a2transit.main import create_app

#: Repo-root data/ directory, where the ingest caches downloaded feeds.
DATA_DIR = Path(__file__).resolve().parents[2] / "data"


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def _test_database_url() -> tuple[str, str, str]:
    """(maintenance_url, test_url, test_db_name) derived from DATABASE_URL.

    Tests get their own database rather than sharing the development one: the
    loader's delete-and-reload would otherwise wipe whatever feeds are loaded
    locally every time the suite ran.
    """
    url = sqlalchemy.engine.make_url(get_settings().database_url)
    test_name = f"{url.database}_test"
    return (
        url.set(database="postgres").render_as_string(hide_password=False),
        url.set(database=test_name).render_as_string(hide_password=False),
        test_name,
    )


@pytest.fixture(scope="session")
def db_engine() -> Iterator[Engine]:
    """Engine against a dedicated test database, created on demand.

    Skips the whole db-marked suite when Postgres is unreachable, so the tests
    stay runnable without `docker compose up`.
    """
    maintenance_url, test_url, test_name = _test_database_url()

    maintenance = create_engine(maintenance_url, isolation_level="AUTOCOMMIT")
    try:
        with maintenance.connect() as connection:
            exists = connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": test_name}
            ).scalar()
            if not exists:
                connection.execute(text(f'CREATE DATABASE "{test_name}"'))
    except sqlalchemy.exc.OperationalError as exc:
        pytest.skip(f"Postgres unavailable ({exc.__class__.__name__}); run docker compose up -d")
    finally:
        maintenance.dispose()

    engine = create_engine(test_url, future=True)
    schema.drop_all(engine)
    schema.create_all(engine)

    yield engine

    engine.dispose()


@pytest.fixture
def clean_db(db_engine: Engine) -> Iterator[Engine]:
    """Empty every table before a test, leaving the schema in place."""
    _truncate_all(db_engine)
    yield db_engine


def _truncate_all(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE agencies, stops, routes, calendar, calendar_dates, shapes, "
                "shape_geometries, trips, stop_times, transfers, feed_versions "
                "RESTART IDENTITY CASCADE"
            )
        )


# --------------------------------------------------------------------------
# Synthetic GTFS feeds
#
# Built in-process rather than committed as fixture files: a real feed is 2.4 MB
# of binary in git for every future change, and a hand-written one makes the
# property under test — a time past midnight, a colliding id — visible in the
# test that relies on it.
# --------------------------------------------------------------------------

MINIMAL_FEED: dict[str, tuple[Sequence[str], Sequence[Sequence[str]]]] = {
    "agency.txt": (
        ["agency_id", "agency_name", "agency_url", "agency_timezone"],
        [["A1", "Test Agency", "https://example.test", "America/Detroit"]],
    ),
    "stops.txt": (
        ["stop_id", "stop_code", "stop_name", "stop_lat", "stop_lon"],
        [
            ["161", "1610", "Colliding Stop", "42.2799", "-83.7466"],
            ["S2", "0002", "Second Stop", "42.2850", "-83.7400"],
        ],
    ),
    "routes.txt": (
        ["route_id", "agency_id", "route_short_name", "route_long_name", "route_type"],
        [["R1", "A1", "1", "Test Route", "3"]],
    ),
    "calendar.txt": (
        [
            "service_id",
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
            "start_date",
            "end_date",
        ],
        [["3", "1", "1", "1", "1", "1", "0", "0", "20260823", "20270130"]],
    ),
    "calendar_dates.txt": (
        ["service_id", "date", "exception_type"],
        [["3", "20261126", "2"]],
    ),
    "shapes.txt": (
        ["shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"],
        [
            ["SH1", "42.2799", "-83.7466", "1"],
            ["SH1", "42.2820", "-83.7430", "2"],
            ["SH1", "42.2850", "-83.7400", "3"],
        ],
    ),
    "trips.txt": (
        ["trip_id", "route_id", "service_id", "trip_headsign", "direction_id", "shape_id"],
        [["T1", "R1", "3", "To Second Stop", "0", "SH1"]],
    ),
    "stop_times.txt": (
        ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"],
        [
            # Deliberately past midnight: this is the value that must survive
            # the round trip through Postgres unwrapped.
            ["T1", "23:50:00", "23:50:00", "161", "1"],
            ["T1", "27:15:00", "27:15:00", "S2", "2"],
        ],
    ),
    "transfers.txt": (
        ["from_stop_id", "to_stop_id", "transfer_type", "min_transfer_time"],
        [["161", "S2", "2", "300"]],
    ),
    "feed_info.txt": (
        [
            "feed_publisher_name",
            "feed_publisher_url",
            "feed_lang",
            "feed_start_date",
            "feed_end_date",
            "feed_version",
        ],
        [["Test", "https://example.test", "en", "20260823", "20270130", "TESTV1"]],
    ),
}


def write_gtfs_zip(
    path: Path,
    files: Mapping[str, tuple[Sequence[str], Sequence[Sequence[str]]]] | None = None,
    *,
    omit: Sequence[str] = (),
) -> Path:
    """Write a GTFS ZIP from header/rows pairs."""
    files = MINIMAL_FEED if files is None else files
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, (header, rows) in files.items():
            if name in omit:
                continue
            buffer = io.StringIO()
            writer = csv.writer(buffer, lineterminator="\n")
            writer.writerow(header)
            writer.writerows(rows)
            archive.writestr(name, buffer.getvalue())
    return path


@pytest.fixture
def minimal_feed(tmp_path: Path) -> Path:
    return write_gtfs_zip(tmp_path / "minimal.zip")
