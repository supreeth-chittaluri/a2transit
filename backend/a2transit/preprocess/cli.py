"""Rebuild the RAPTOR pattern tables.

    python -m a2transit.preprocess
    python -m a2transit.preprocess --agency theride

Runs automatically at the end of a successful ingest, so this is for rebuilding
patterns on their own — after changing the pattern logic, say. The tables are a
pure function of the GTFS tables, so running it twice is a no-op beyond the
work.
"""

from __future__ import annotations

import argparse
import logging
import sys

from sqlalchemy import Engine

from a2transit.db.models import AgencySource
from a2transit.db.session import get_engine
from a2transit.preprocess.patterns import PatternBuildResult, build_patterns

logger = logging.getLogger("a2transit.preprocess")


def run(engine: Engine, agencies: tuple[AgencySource, ...]) -> list[PatternBuildResult]:
    return [build_patterns(engine, agency) for agency in agencies]


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

    try:
        results = run(get_engine(), agencies)
    except Exception:
        logger.exception("pattern build failed")
        logger.error("has the feed been ingested? run `python -m a2transit.ingest` first")
        return 1

    print_summary(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
