"""Command-line entry point for GTFS ingest.

    python -m a2transit.ingest                    # refresh both agencies
    python -m a2transit.ingest --agency theride   # just one
    python -m a2transit.ingest --force            # reload even if unchanged
    python -m a2transit.ingest --from-file f.zip --agency mbus

Safe to run on a schedule. Unchanged feeds are revalidated with a conditional
GET and skipped without touching the database, so a weekly cron entry costs
almost nothing:

    0 4 * * 1  cd /path/to/a2transit/backend && .venv/bin/python -m a2transit.ingest

TheRide's licence requires refreshing within three business days of a new
publication, so weekly is the floor rather than a target.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import httpx
from sqlalchemy import Engine, inspect

from a2transit.db.models import TABLES_IN_DEPENDENCY_ORDER, AgencySource
from a2transit.db.schema import create_all
from a2transit.db.session import get_engine
from a2transit.ingest.feeds import download_feed, feed_spec_for, feed_specs, local_feed
from a2transit.ingest.loader import FeedFormatError, LoadResult, load_feed
from a2transit.preprocess.cli import print_footpath_summary
from a2transit.preprocess.cli import print_summary as print_pattern_summary
from a2transit.preprocess.cli import run as preprocess_run
from a2transit.preprocess.footpaths import build_footpaths

logger = logging.getLogger("a2transit.ingest")

DEFAULT_CACHE_DIR = Path(__file__).resolve().parents[2].parent / "data"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m a2transit.ingest",
        description="Load TheRide and U-M MBus GTFS feeds into Postgres.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--agency",
        choices=[source.value for source in AgencySource],
        help="Load only this agency. Default: both.",
    )
    parser.add_argument(
        "--from-file",
        type=Path,
        metavar="PATH",
        help="Load a local GTFS ZIP instead of downloading. Requires --agency.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reload even when the feed content is unchanged since the last load.",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=DEFAULT_CACHE_DIR,
        help=f"Where downloaded feeds are cached (default: {DEFAULT_CACHE_DIR}).",
    )
    parser.add_argument(
        "--no-preprocess",
        action="store_true",
        help="Skip rebuilding RAPTOR patterns after loading.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Log every table load.")

    args = parser.parse_args(argv)
    if args.from_file and not args.agency:
        parser.error("--from-file requires --agency, since a ZIP does not say who published it")
    return args


def _ensure_schema(engine: Engine) -> None:
    """Create tables on first run so a fresh database needs no separate step."""
    existing = set(inspect(engine).get_table_names())
    expected = {model.__tablename__ for model in TABLES_IN_DEPENDENCY_ORDER}
    if expected <= existing:
        return

    logger.info("creating schema (%d tables missing)", len(expected - existing))
    create_all(engine)


def _print_summary(results: list[LoadResult]) -> None:
    """The M1 acceptance output: row counts per table, per agency."""
    loaded = [result for result in results if not result.skipped]
    if not loaded:
        print("\nNothing to do — every feed already loaded. Use --force to reload.")
        return

    tables = sorted({table for result in loaded for table in result.row_counts})
    headers = [result.agency_source.value for result in loaded]

    width = max(len(table) for table in tables) + 2
    print()
    print(f"{'table':<{width}}" + "".join(f"{header:>14}" for header in headers) + f"{'total':>14}")
    print("-" * (width + 14 * (len(headers) + 1)))

    for table in tables:
        counts = [result.row_counts.get(table, 0) for result in loaded]
        row = "".join(f"{count:>14,}" for count in counts)
        print(f"{table:<{width}}{row}{sum(counts):>14,}")

    totals = [result.total_rows for result in loaded]
    print("-" * (width + 14 * (len(headers) + 1)))
    print(
        f"{'TOTAL':<{width}}"
        + "".join(f"{total:>14,}" for total in totals)
        + f"{sum(totals):>14,}"
    )
    print()
    for result in loaded:
        window = ""
        if result.feed_start_date and result.feed_end_date:
            window = f", service {result.feed_start_date} to {result.feed_end_date}"
        print(
            f"  {result.agency_source.value}: feed_version {result.feed_version or 'n/a'}"
            f"{window} — loaded in {result.seconds:.1f}s"
        )
    for result in results:
        if result.skipped:
            print(f"  {result.agency_source.value}: unchanged, skipped")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(message)s",
    )
    # SQLAlchemy's engine chatter drowns out our own logging at DEBUG.
    logging.getLogger("sqlalchemy").setLevel(logging.WARNING)

    engine = get_engine()
    try:
        _ensure_schema(engine)
    except Exception:
        logger.exception("could not reach the database — is `docker compose up -d` running?")
        return 1

    if args.agency:
        specs = (feed_spec_for(AgencySource(args.agency)),)
    else:
        specs = feed_specs()

    results: list[LoadResult] = []
    with httpx.Client(timeout=120.0, follow_redirects=True) as client:
        for spec in specs:
            try:
                if args.from_file:
                    if not args.from_file.exists():
                        logger.error("no such file: %s", args.from_file)
                        return 1
                    downloaded = local_feed(spec, args.from_file)
                    # An explicitly supplied file is always meant to be loaded.
                    force = True
                else:
                    downloaded = download_feed(
                        spec, args.cache_dir, force=args.force, client=client
                    )
                    force = args.force

                results.append(load_feed(engine, downloaded, force=force))
            except httpx.HTTPError:
                logger.exception("%s: download failed", spec.label)
                logger.error("check docs/feeds.md — both agencies have moved their feed before")
                return 1
            except FeedFormatError:
                logger.exception("%s: feed is not loadable", spec.label)
                return 1

    _print_summary(results)

    # Patterns are derived from the GTFS tables, so a load that changed those
    # tables leaves them stale. Rebuilding here rather than as a separate step
    # people have to remember means the database is never briefly inconsistent
    # in a way the router would silently use.
    if not args.no_preprocess and any(not result.skipped for result in results):
        rebuilt = preprocess_run(engine, tuple(result.agency_source for result in results))
        print_pattern_summary(rebuilt)
        # Footpaths are rebuilt whole even when one agency was loaded: a link's
        # two ends can belong to different feeds, so there is no such thing as
        # one agency's half of the table.
        print_footpath_summary(build_footpaths(engine))

    return 0


if __name__ == "__main__":
    sys.exit(main())
