"""Loader behaviour against a real Postgres.

Marked `db`; the db_engine fixture skips the lot when Postgres is unreachable,
so this suite still runs (as skips) without `docker compose up`.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from a2transit.db.models import AgencySource
from a2transit.ingest.loader import FeedFormatError, load_from_path
from tests.conftest import MINIMAL_FEED, write_gtfs_zip

pytestmark = pytest.mark.db


def _counts(engine: Engine, agency: AgencySource) -> dict[str, int]:
    tables = (
        "agencies",
        "stops",
        "routes",
        "calendar",
        "calendar_dates",
        "shapes",
        "shape_geometries",
        "trips",
        "stop_times",
        "transfers",
    )
    with engine.connect() as connection:
        return {
            table: connection.execute(
                text(f"SELECT count(*) FROM {table} WHERE agency_source = :s"),
                {"s": agency.value},
            ).scalar_one()
            for table in tables
        }


class TestLoad:
    def test_loads_every_table(self, clean_db: Engine, minimal_feed: Path) -> None:
        result = load_from_path(clean_db, AgencySource.THERIDE, minimal_feed)

        assert result.row_counts == {
            "agencies": 1,
            "stops": 2,
            "routes": 1,
            "calendar": 1,
            "calendar_dates": 1,
            "shapes": 3,
            "trips": 1,
            "stop_times": 2,
            "transfers": 1,
            "shape_geometries": 1,
        }
        assert result.feed_version == "TESTV1"

    def test_times_past_midnight_survive_the_round_trip(
        self, clean_db: Engine, minimal_feed: Path
    ) -> None:
        """The failure this whole schema decision exists to prevent."""
        load_from_path(clean_db, AgencySource.THERIDE, minimal_feed)

        with clean_db.connect() as connection:
            departures = connection.execute(
                text(
                    "SELECT departure_time FROM stop_times "
                    "WHERE agency_source = 'theride' ORDER BY stop_sequence"
                )
            ).scalars().all()

        assert departures == [85800, 98100]  # 23:50:00 and 27:15:00
        # Not wrapped to 03:15, which would sort the trip's end before its start.
        assert departures[1] > departures[0]

    def test_stop_geography_is_populated(self, clean_db: Engine, minimal_feed: Path) -> None:
        load_from_path(clean_db, AgencySource.THERIDE, minimal_feed)

        with clean_db.connect() as connection:
            missing, total = connection.execute(
                text(
                    "SELECT count(*) FILTER (WHERE geog IS NULL), count(*) "
                    "FROM stops WHERE agency_source = 'theride'"
                )
            ).one()

        assert (missing, total) == (0, 2)

    def test_shape_geometry_is_a_linestring_of_every_point(
        self, clean_db: Engine, minimal_feed: Path
    ) -> None:
        load_from_path(clean_db, AgencySource.THERIDE, minimal_feed)

        with clean_db.connect() as connection:
            geom_type, points = connection.execute(
                text(
                    "SELECT ST_GeometryType(geom), point_count FROM shape_geometries "
                    "WHERE agency_source = 'theride' AND shape_id = 'SH1'"
                )
            ).one()

        assert geom_type == "ST_LineString"
        assert points == 3


class TestIdempotency:
    def test_reloading_the_same_feed_changes_nothing(
        self, clean_db: Engine, minimal_feed: Path
    ) -> None:
        load_from_path(clean_db, AgencySource.THERIDE, minimal_feed)
        before = _counts(clean_db, AgencySource.THERIDE)

        load_from_path(clean_db, AgencySource.THERIDE, minimal_feed)
        after = _counts(clean_db, AgencySource.THERIDE)

        assert before == after

    def test_removed_rows_do_not_survive_a_reload(
        self, clean_db: Engine, tmp_path: Path
    ) -> None:
        """The reason this is delete-and-reload rather than upsert.

        An upsert would leave the retired stop behind, where M4 would happily
        generate footpaths to a stop the agency no longer serves.
        """
        load_from_path(clean_db, AgencySource.THERIDE, write_gtfs_zip(tmp_path / "before.zip"))

        # Republish without the second stop, the stop_time that visits it, or
        # the transfer to it — as an agency does when it retires a stop.
        shrunk = copy.deepcopy(MINIMAL_FEED)
        for name in ("stops.txt", "stop_times.txt"):
            header, rows = shrunk[name]
            shrunk[name] = (header, rows[:1])
        shrunk["transfers.txt"] = (shrunk["transfers.txt"][0], [])

        load_from_path(
            clean_db, AgencySource.THERIDE, write_gtfs_zip(tmp_path / "after.zip", shrunk)
        )

        with clean_db.connect() as connection:
            stop_ids = connection.execute(
                text("SELECT stop_id FROM stops WHERE agency_source = 'theride' ORDER BY stop_id")
            ).scalars().all()

        assert stop_ids == ["161"]

    def test_skips_when_content_is_unchanged(self, clean_db: Engine, minimal_feed: Path) -> None:
        load_from_path(clean_db, AgencySource.THERIDE, minimal_feed)

        second = load_from_path(clean_db, AgencySource.THERIDE, minimal_feed, force=False)

        assert second.skipped is True
        assert second.row_counts == {}


class TestAgencyNamespacing:
    def test_colliding_ids_coexist_as_different_rows(
        self, clean_db: Engine, minimal_feed: Path
    ) -> None:
        """stop_id 161 and service_id 3 exist in both real feeds meaning different things."""
        load_from_path(clean_db, AgencySource.THERIDE, minimal_feed)
        load_from_path(clean_db, AgencySource.MBUS, minimal_feed)

        with clean_db.connect() as connection:
            stops = connection.execute(
                text(
                    "SELECT agency_source::text FROM stops "
                    "WHERE stop_id = '161' ORDER BY agency_source::text"
                )
            ).scalars().all()
            services = connection.execute(
                text("SELECT count(*) FROM calendar WHERE service_id = '3'")
            ).scalar_one()

        assert stops == ["mbus", "theride"]
        assert services == 2

    def test_reloading_one_agency_leaves_the_other_untouched(
        self, clean_db: Engine, minimal_feed: Path
    ) -> None:
        load_from_path(clean_db, AgencySource.THERIDE, minimal_feed)
        load_from_path(clean_db, AgencySource.MBUS, minimal_feed)
        mbus_before = _counts(clean_db, AgencySource.MBUS)

        load_from_path(clean_db, AgencySource.THERIDE, minimal_feed)

        assert _counts(clean_db, AgencySource.MBUS) == mbus_before


class TestFailureHandling:
    def test_missing_required_file_is_rejected(self, clean_db: Engine, tmp_path: Path) -> None:
        broken = write_gtfs_zip(tmp_path / "no_stops.zip", omit=["stops.txt"])

        with pytest.raises(FeedFormatError, match="stops.txt"):
            load_from_path(clean_db, AgencySource.THERIDE, broken)

    def test_optional_file_may_be_absent(self, clean_db: Engine, tmp_path: Path) -> None:
        feed = write_gtfs_zip(tmp_path / "no_transfers.zip", omit=["transfers.txt"])

        result = load_from_path(clean_db, AgencySource.THERIDE, feed)

        assert result.row_counts["transfers"] == 0
        assert result.row_counts["stop_times"] == 2

    def test_malformed_time_names_the_file_and_line(
        self, clean_db: Engine, tmp_path: Path
    ) -> None:
        bad = copy.deepcopy(MINIMAL_FEED)
        bad["stop_times.txt"] = (
            MINIMAL_FEED["stop_times.txt"][0],
            [["T1", "not-a-time", "23:50:00", "161", "1"]],
        )

        with pytest.raises(FeedFormatError, match=r"stop_times\.txt line 2"):
            load_from_path(
                clean_db, AgencySource.THERIDE, write_gtfs_zip(tmp_path / "bad.zip", bad)
            )

    def test_a_failed_load_leaves_the_previous_feed_intact(
        self, clean_db: Engine, minimal_feed: Path, tmp_path: Path
    ) -> None:
        """Everything is one transaction, so a bad feed cannot half-replace a good one."""
        load_from_path(clean_db, AgencySource.THERIDE, minimal_feed)
        before = _counts(clean_db, AgencySource.THERIDE)

        bad = copy.deepcopy(MINIMAL_FEED)
        bad["stop_times.txt"] = (
            MINIMAL_FEED["stop_times.txt"][0],
            [["T1", "25:99:00", "23:50:00", "161", "1"]],
        )
        with pytest.raises(FeedFormatError):
            load_from_path(
                clean_db, AgencySource.THERIDE, write_gtfs_zip(tmp_path / "bad.zip", bad)
            )

        assert _counts(clean_db, AgencySource.THERIDE) == before

    def test_non_zip_is_rejected(self, clean_db: Engine, tmp_path: Path) -> None:
        not_a_zip = tmp_path / "nope.zip"
        not_a_zip.write_text("<html>404 Not Found</html>")

        with pytest.raises(FeedFormatError, match="not a ZIP"):
            load_from_path(clean_db, AgencySource.THERIDE, not_a_zip)
