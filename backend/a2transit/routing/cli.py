"""Command-line trip planner, for checking itineraries by hand.

    python -m a2transit.routing --from 1338 --to 1605 --depart 2026-09-10T06:00
    python -m a2transit.routing --search "Blake"
    python -m a2transit.routing --from theride:544 --to theride:1019 \
        --depart 2026-09-10T09:00 --verbose

Stops are given as `agency:stop_id` or a bare `stop_id`. A bare id is rejected
when both feeds use it — 90 of them do, and they are different places, so
guessing would silently plan a journey in the wrong city district.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
import time

from sqlalchemy import Engine, text

from a2transit.db.models import AgencySource
from a2transit.db.session import get_engine
from a2transit.routing.graph import DEFAULT_HORIZON_SECONDS
from a2transit.routing.search import PlanningError, plan
from a2transit.routing.timetable import StopKey, build_timetable

logger = logging.getLogger("a2transit.routing")


class StopReferenceError(PlanningError):
    """A stop reference could not be resolved to exactly one stop."""


def resolve_stop(engine: Engine, reference: str) -> StopKey:
    """Turn `agency:stop_id` or a bare `stop_id` into a (agency, stop_id) key."""
    agency: AgencySource | None = None
    stop_id = reference

    if ":" in reference:
        prefix, stop_id = reference.split(":", 1)
        try:
            agency = AgencySource(prefix)
        except ValueError:
            valid = ", ".join(source.value for source in AgencySource)
            raise StopReferenceError(
                f"unknown agency {prefix!r} (expected one of {valid})"
            ) from None

    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT agency_source, stop_id, stop_name FROM stops "
                "WHERE stop_id = :stop_id"
                + (" AND agency_source = :agency" if agency else "")
            ),
            {"stop_id": stop_id, **({"agency": agency.value} if agency else {})},
        ).all()

    if not rows:
        raise StopReferenceError(f"no stop with id {stop_id!r}")
    if len(rows) > 1:
        options = ", ".join(f"{row.agency_source}:{row.stop_id} ({row.stop_name})" for row in rows)
        raise StopReferenceError(
            f"stop id {stop_id!r} exists in both feeds as different places — "
            f"qualify it: {options}"
        )
    return (AgencySource(rows[0].agency_source), rows[0].stop_id)


def search_stops(engine: Engine, query: str, limit: int = 25) -> None:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT agency_source, stop_id, stop_name, stop_code FROM stops "
                "WHERE stop_name ILIKE :pattern ORDER BY stop_name LIMIT :limit"
            ),
            {"pattern": f"%{query}%", "limit": limit},
        ).all()

    if not rows:
        print(f"No stop matching {query!r}.")
        return

    width = max(len(f"{row.agency_source}:{row.stop_id}") for row in rows)
    for row in rows:
        reference = f"{row.agency_source}:{row.stop_id}"
        print(f"  {reference:<{width}}  {row.stop_name}")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m a2transit.routing",
        description="Plan a trip with the M2 reference engine.",
    )
    parser.add_argument("--search", metavar="TEXT", help="List stops whose name contains TEXT.")
    parser.add_argument("--from", dest="origin", metavar="STOP", help="Origin, agency:stop_id.")
    parser.add_argument("--to", dest="destination", metavar="STOP", help="Destination.")
    parser.add_argument(
        "--depart",
        metavar="ISO8601",
        help="Departure time, e.g. 2026-09-10T09:00. Defaults to now.",
    )
    parser.add_argument(
        "--horizon",
        type=int,
        default=DEFAULT_HORIZON_SECONDS // 3600,
        metavar="HOURS",
        help=f"How far ahead to look (default: {DEFAULT_HORIZON_SECONDS // 3600}).",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Show search statistics.")

    args = parser.parse_args(argv)
    if not args.search and not (args.origin and args.destination):
        parser.error("give --search, or both --from and --to")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)-7s %(message)s",
    )

    engine = get_engine()

    if args.search:
        search_stops(engine, args.search)
        return 0

    departure = dt.datetime.fromisoformat(args.depart) if args.depart else dt.datetime.now()

    try:
        origin = resolve_stop(engine, args.origin)
        destination = resolve_stop(engine, args.destination)
    except StopReferenceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("hint: find a stop with --search", file=sys.stderr)
        return 2

    build_started = time.perf_counter()
    timetable = build_timetable(engine, departure.date())
    build_seconds = time.perf_counter() - build_started

    query_started = time.perf_counter()
    result = plan(
        engine,
        origin,
        destination,
        departure,
        horizon_seconds=args.horizon * 3600,
        timetable=timetable,
    )
    query_seconds = time.perf_counter() - query_started

    if result.itinerary is None:
        origin_stop = timetable.stops[origin]
        destination_stop = timetable.stops[destination]
        print(
            f"No itinerary from {origin_stop.name} to {destination_stop.name} "
            f"departing {departure:%a %Y-%m-%d %H:%M} "
            f"within {args.horizon}h."
        )
        print("The stops may be unconnected, or nothing may be running at that time.")
        return 1

    print(result.itinerary.describe())

    if args.verbose:
        print()
        print(f"  timetable built in {build_seconds * 1000:.0f} ms")
        print(f"  query answered in  {query_seconds * 1000:.0f} ms")
        print(f"  {result.stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
