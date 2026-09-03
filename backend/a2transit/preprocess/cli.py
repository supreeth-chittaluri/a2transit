"""Rebuild the derived routing tables: RAPTOR patterns and PostGIS footpaths.

    python -m a2transit.preprocess
    python -m a2transit.preprocess --agency theride
    python -m a2transit.preprocess --footpaths-only

Runs automatically at the end of a successful ingest, so this is for rebuilding
on its own — after changing the pattern logic or the walking-speed constants,
say. Both outputs are a pure function of the GTFS tables, so running it twice is
a no-op beyond the work.

Patterns are per-agency; footpaths are not, and cannot be. A footpath row names
a stop in each agency, so rebuilding "just TheRide's" would have to decide what
to do with the 1,456 links whose other end is MBus. It rebuilds whole, always.
"""

from __future__ import annotations

import argparse
import logging
import sys

from sqlalchemy import Engine

from a2transit.db.models import AgencySource
from a2transit.db.session import get_engine
from a2transit.preprocess.footpaths import FootpathBuildResult, build_footpaths
from a2transit.preprocess.patterns import PatternBuildResult, build_patterns

logger = logging.getLogger("a2transit.preprocess")


def run(engine: Engine, agencies: tuple[AgencySource, ...]) -> list[PatternBuildResult]:
    return [build_patterns(engine, agency) for agency in agencies]


def print_footpath_summary(result: FootpathBuildResult) -> None:
    print()
    print(
        f"footpaths    {result.total:,} links "
        f"({result.within_agency:,} within an agency, "
        f"{result.cross_agency:,} across), "
        f"longest {result.max_metres:.0f} m, {result.seconds:.1f}s"
    )
    print(
        f"             {result.declared:,} also declared by an agency"
        + (
            f", {result.beyond_radius:,} kept only because it is declared"
            if result.beyond_radius
            else ""
        )
    )


def print_summary(results: list[PatternBuildResult]) -> None:
    if not results:
        return

    print()
    print(f"{'agency':<12}{'patterns':>12}{'pattern stops':>16}{'trips':>10}{'seconds':>10}")
    print("-" * 60)
    for result in results:
        print(
            f"{result.agency_source.value:<12}"
            f"{result.pattern_count:>12,}"
            f"{result.pattern_stop_count:>16,}"
            f"{result.trip_count:>10,}"
            f"{result.seconds:>10.1f}"
        )
    print("-" * 60)
    print(
        f"{'TOTAL':<12}"
        f"{sum(r.pattern_count for r in results):>12,}"
        f"{sum(r.pattern_stop_count for r in results):>16,}"
        f"{sum(r.trip_count for r in results):>10,}"
        f"{sum(r.seconds for r in results):>10.1f}"
    )

    flagged = [
        pattern for result in results for pattern in result.overtaking_patterns
    ]
    if flagged:
        print()
        print(
            f"  {len(flagged)} pattern(s) have trips that overtake within a service day "
            f"and will be scanned rather than binary-searched:"
        )
        for pattern in sorted(flagged):
            print(f"    {pattern}")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m a2transit.preprocess",
        description="Rebuild RAPTOR route patterns from the loaded GTFS tables.",
    )
    parser.add_argument(
        "--agency",
        choices=[source.value for source in AgencySource],
        help="Rebuild only this agency. Default: both.",
    )
    parser.add_argument(
        "--footpaths-only",
        action="store_true",
        help="Rebuild footpaths without touching the pattern tables.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(message)s",
    )
    logging.getLogger("sqlalchemy").setLevel(logging.WARNING)

    agencies = (
        (AgencySource(args.agency),) if args.agency else tuple(AgencySource)
    )

    engine = get_engine()
    try:
        if not args.footpaths_only:
            print_summary(run(engine, agencies))
        print_footpath_summary(build_footpaths(engine))
    except Exception:
        logger.exception("preprocessing failed")
        logger.error("has the feed been ingested? run `python -m a2transit.ingest` first")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
