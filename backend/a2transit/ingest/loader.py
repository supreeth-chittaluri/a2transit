"""Load a GTFS ZIP into Postgres.

Idempotency is by delete-and-reload, per agency, inside one transaction: a feed
is a snapshot rather than a delta, so an upsert would leave behind stops the
agency has since retired, and those would linger as phantom transfer points once
M4 starts generating footpaths. Deleting first makes the loaded state a function
of the current feed alone.

The cost is a window inside the transaction where one agency's tables are empty.
Readers on other connections never see it — the delete and the reload commit
together — but a long-running query started before the refresh can block it.
Acceptable while nothing is serving live traffic; revisit with a shadow schema
at M8.

The other agency's rows are never touched, so a TheRide refresh cannot disturb
MBus data.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from a2transit.db.models import (
    TABLES_IN_DEPENDENCY_ORDER,
    AgencySource,
    agency_row_predicate,
)
from a2transit.ingest.feeds import DownloadedFeed
from a2transit.ingest.fields import GtfsFieldError, parse_gtfs_date, parse_text
from a2transit.ingest.tables import TABLE_SPECS, TableSpec

logger = logging.getLogger(__name__)

#: Children before parents, so deletes satisfy foreign keys.
#
# Derived from the models rather than written out again. It was written out
# again once, and adding the RAPTOR pattern tables — which carry foreign keys
# to trips and stops — left this list stale, so the next reload failed on a
# foreign key violation. One list, one place to update.
_DELETE_ORDER: tuple[str, ...] = tuple(
    model.__tablename__ for model in TABLES_IN_DEPENDENCY_ORDER
)


class FeedFormatError(RuntimeError):
    """The ZIP is not a usable GTFS feed."""


@dataclass
class LoadResult:
    agency_source: AgencySource
    row_counts: dict[str, int]
    feed_version: str | None
    feed_start_date: object | None
    feed_end_date: object | None
    seconds: float
    skipped: bool = False

    @property
    def total_rows(self) -> int:
        return sum(self.row_counts.values())


def _open_text(archive: zipfile.ZipFile, name: str) -> IO[str]:
    # utf-8-sig: both agencies emit a BOM, which would otherwise become part of
    # the first header name and silently break every lookup on that column.
    return io.TextIOWrapper(archive.open(name), encoding="utf-8-sig", newline="")


def _read_feed_info(archive: zipfile.ZipFile) -> dict[str, object]:
    if "feed_info.txt" not in archive.namelist():
        return {}

    with _open_text(archive, "feed_info.txt") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        return {}

    row = rows[0]
    return {
        "feed_version": parse_text(row.get("feed_version")),
        "feed_start_date": parse_gtfs_date(row.get("feed_start_date"), field="feed_start_date"),
        "feed_end_date": parse_gtfs_date(row.get("feed_end_date"), field="feed_end_date"),
    }


def _copy_table(
    connection: Connection,
    archive: zipfile.ZipFile,
    spec: TableSpec,
    agency_source: AgencySource,
) -> int:
    if spec.gtfs_file not in archive.namelist():
        if spec.required:
            raise FeedFormatError(f"feed is missing required file {spec.gtfs_file}")
        logger.debug("%s absent, skipping", spec.gtfs_file)
        return 0

    column_list = ", ".join(spec.columns)
    statement = f"COPY {spec.table} ({column_list}) FROM STDIN"

    # psycopg's COPY lives on the raw driver connection, but it runs on the same
    # connection SQLAlchemy has open, so it joins the surrounding transaction.
    driver_connection = connection.connection.driver_connection
    written = 0

    with _open_text(archive, spec.gtfs_file) as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            return 0

        with driver_connection.cursor() as cursor, cursor.copy(statement) as copy:
            for line_number, row in enumerate(reader, start=2):
                if spec.skip_if is not None and spec.skip_if(row):
                    continue
                try:
                    copy.write_row(spec.row_factory(row, agency_source))
                except GtfsFieldError as exc:
                    raise FeedFormatError(
                        f"{spec.gtfs_file} line {line_number}: {exc}"
                    ) from exc
                written += 1

    return written


def _derive_geometries(connection: Connection, agency_source: AgencySource) -> int:
    """Build stop points and per-shape LineStrings after the raw rows land.

    Done in SQL rather than during COPY because PostGIS can do it in two set
    operations, and because COPY has no clean way to write a geography value.
    """
    connection.execute(
        text(
            """
            UPDATE stops
               SET geog = ST_SetSRID(ST_MakePoint(stop_lon, stop_lat), 4326)::geography
             WHERE agency_source = :source
            """
        ),
        {"source": agency_source.value},
    )

    result = connection.execute(
        text(
            """
            INSERT INTO shape_geometries (agency_source, shape_id, geom, point_count)
            SELECT agency_source,
                   shape_id,
                   ST_MakeLine(
                       ST_SetSRID(ST_MakePoint(shape_pt_lon, shape_pt_lat), 4326)
                       ORDER BY shape_pt_sequence
                   ),
                   COUNT(*)
              FROM shapes
             WHERE agency_source = :source
             GROUP BY agency_source, shape_id
            -- ST_MakeLine needs two distinct points; a one-point shape is
            -- malformed upstream and would abort the whole load.
            HAVING COUNT(*) > 1
            """
        ),
        {"source": agency_source.value},
    )
    return result.rowcount or 0


def _delete_agency(connection: Connection, agency_source: AgencySource) -> None:
    for table in _DELETE_ORDER:
        connection.execute(
            text(f"DELETE FROM {table} WHERE {agency_row_predicate(table)}"),
            {"source": agency_source.value},
        )


def _record_feed_version(
    connection: Connection,
    downloaded: DownloadedFeed,
    result: LoadResult,
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO feed_versions (
                agency_source, feed_version, feed_start_date, feed_end_date,
                source_url, content_sha256, content_length, last_modified,
                fetched_at, load_seconds, row_counts
            ) VALUES (
                :agency_source, :feed_version, :feed_start_date, :feed_end_date,
                :source_url, :content_sha256, :content_length, :last_modified,
                :fetched_at, :load_seconds, CAST(:row_counts AS jsonb)
            )
            """
        ),
        {
            "agency_source": downloaded.spec.agency_source.value,
            "feed_version": result.feed_version,
            "feed_start_date": result.feed_start_date,
            "feed_end_date": result.feed_end_date,
            "source_url": downloaded.spec.gtfs_url,
            "content_sha256": downloaded.sha256,
            "content_length": downloaded.content_length,
            "last_modified": downloaded.last_modified,
            "fetched_at": downloaded.fetched_at,
            "load_seconds": result.seconds,
            "row_counts": json.dumps(result.row_counts),
        },
    )


def already_loaded(engine: Engine, downloaded: DownloadedFeed) -> bool:
    """True when this exact ZIP is the most recent successful load for its agency."""
    with engine.connect() as connection:
        latest = connection.execute(
            text(
                """
                SELECT content_sha256
                  FROM feed_versions
                 WHERE agency_source = :source
                 ORDER BY loaded_at DESC
                 LIMIT 1
                """
            ),
            {"source": downloaded.spec.agency_source.value},
        ).scalar_one_or_none()
    return latest == downloaded.sha256


def load_feed(
    engine: Engine,
    downloaded: DownloadedFeed,
    *,
    force: bool = False,
) -> LoadResult:
    """Replace one agency's data with the contents of `downloaded`.

    Everything happens in a single transaction: the delete, every COPY, the
    derived geometries, and the feed_versions row commit together or not at all.
    A failure part-way leaves the previous feed intact.
    """
    agency_source = downloaded.spec.agency_source

    if not force and already_loaded(engine, downloaded):
        logger.info(
            "%s: already loaded (sha256 %s), skipping",
            downloaded.spec.label,
            downloaded.sha256[:12],
        )
        return LoadResult(
            agency_source=agency_source,
            row_counts={},
            feed_version=None,
            feed_start_date=None,
            feed_end_date=None,
            seconds=0.0,
            skipped=True,
        )

    if not zipfile.is_zipfile(downloaded.path):
        raise FeedFormatError(f"{downloaded.path} is not a ZIP archive")

    started = time.perf_counter()
    row_counts: dict[str, int] = {}

    with zipfile.ZipFile(downloaded.path) as archive, engine.begin() as connection:
        feed_info = _read_feed_info(archive)

        _delete_agency(connection, agency_source)

        for spec in TABLE_SPECS:
            count = _copy_table(connection, archive, spec, agency_source)
            row_counts[spec.table] = count
            logger.debug("%s: %s <- %d rows", agency_source.value, spec.table, count)

        row_counts["shape_geometries"] = _derive_geometries(connection, agency_source)

        result = LoadResult(
            agency_source=agency_source,
            row_counts=row_counts,
            feed_version=feed_info.get("feed_version"),  # type: ignore[arg-type]
            feed_start_date=feed_info.get("feed_start_date"),
            feed_end_date=feed_info.get("feed_end_date"),
            seconds=time.perf_counter() - started,
        )
        _record_feed_version(connection, downloaded, result)

    logger.info(
        "%s: loaded %d rows in %.1fs",
        downloaded.spec.label,
        result.total_rows,
        result.seconds,
    )
    return result


def load_from_path(
    engine: Engine,
    agency_source: AgencySource,
    path: Path,
    *,
    force: bool = True,
) -> LoadResult:
    """Convenience wrapper for a ZIP already on disk. Used by --from-file and tests."""
    from a2transit.ingest.feeds import feed_spec_for, local_feed

    return load_feed(engine, local_feed(feed_spec_for(agency_source), path), force=force)
