"""Command-line trip planner, for checking itineraries by hand.

    python -m a2transit.routing --from 1338 --to 1605 --depart 2026-09-10T06:00
    python -m a2transit.routing --search "Blake"
    python -m a2transit.routing --from theride:544 --to theride:1019 \
        --depart 2026-09-10T09:00 --verbose
    python -m a2transit.routing --from-place "Kerrytown, Ann Arbor" \
        --to-place "Michigan Stadium" --depart 2026-09-10T09:00
    python -m a2transit.routing --from-latlon 42.2846,-83.7454 --to-latlon 42.2658,-83.7486

Stops are given as `agency:stop_id` or a bare `stop_id`. A bare id is rejected
when both feeds use it — 90 of them do, and they are different places, so
guessing would silently plan a journey in the wrong city district.

An endpoint may equally be an address (`--from-place`, geocoded) or a
coordinate pair (`--from-latlon`). Either becomes a stop no vehicle serves,
joined to the network by footpaths, so the engines need no notion of a place.
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
from a2transit.geocode import GeocodingError, geocode, parse_latlon
from a2transit.routing.compare import Case, compare_cases
from a2transit.routing.engine import plan_with_raptor
from a2transit.routing.graph import DEFAULT_HORIZON_SECONDS
from a2transit.routing.patterns import build_raptor_timetable
from a2transit.routing.places import (
    ACCESS_MAX_METRES,
    Place,
    PlaceAttachment,
    attach_places,
    with_places,
    with_places_raptor,
)
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
    parser.add_argument("--from-place", metavar="ADDRESS", help="Origin as an address.")
    parser.add_argument("--to-place", metavar="ADDRESS", help="Destination as an address.")
    parser.add_argument("--from-latlon", metavar="LAT,LON", help="Origin as coordinates.")
    parser.add_argument("--to-latlon", metavar="LAT,LON", help="Destination as coordinates.")
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
    parser.add_argument(
        "--algorithm",
        choices=("raptor", "dijkstra"),
        default="raptor",
        help="Which engine to plan with (default: raptor).",
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="Run both engines and report whether they agree. Prints the "
        "reproduction command differential test failures quote.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Show search statistics.")

    args = parser.parse_args(argv)
    has_origin = args.origin or args.from_place or args.from_latlon
    has_destination = args.destination or args.to_place or args.to_latlon
    if not args.search and not (has_origin and has_destination):
        parser.error(
            "give --search, or an origin (--from / --from-place / --from-latlon) "
            "and a destination (--to / --to-place / --to-latlon)"
        )
    return args


def _resolve_place(address: str | None, latlon: str | None) -> Place | None:
    """An endpoint given as a place rather than a stop, if there is one."""
    if latlon:
        return parse_latlon(latlon)
    if address:
        result = geocode(address)
        logger.debug("geocoded %r via %s to %r", address, result.provider, result.place)
        return result.place
    return None


def _plan_with_raptor(
    engine: Engine,
    origin: StopKey,
    destination: StopKey,
    departure: dt.datetime,
    *,
    verbose: bool,
    attachment: PlaceAttachment | None = None,
) -> int:
    """RAPTOR returns every non-dominated option, so print the whole set."""
    build_started = time.perf_counter()
    timetable = build_raptor_timetable(engine, departure.date())
    if attachment is not None:
        timetable = with_places_raptor(timetable, attachment)
    build_seconds = time.perf_counter() - build_started

    outcome = plan_with_raptor(timetable, origin, destination, departure)

    if not outcome.itineraries:
        print(
            f"No itinerary from {timetable.stops[origin].name} to "
            f"{timetable.stops[destination].name} departing {departure:%a %Y-%m-%d %H:%M}."
        )
        return 1

    for index, itinerary in enumerate(outcome.itineraries):
        if index:
            print()
        print(itinerary.describe())

    if len(outcome.itineraries) > 1:
        print()
        print(
            f"  {len(outcome.itineraries)} options: fewest transfers arrives "
            f"{outcome.fewest_transfers.arrival:%H:%M}, fastest arrives "
            f"{outcome.fastest.arrival:%H:%M}"
        )
    if verbose:
        print()
        print(f"  timetable built in {build_seconds * 1000:.0f} ms")
        print(f"  query answered in  {outcome.seconds * 1000:.2f} ms")
    return 0


def _compare(
    engine: Engine,
    origin: StopKey,
    destination: StopKey,
    departure: dt.datetime,
    *,
    attachment: PlaceAttachment | None = None,
) -> int:
    """Run both engines on one query and say whether they agree."""
    case = Case(0, origin, destination, departure)
    comparison = compare_cases(engine, (case,), attachment=attachment)[0]

    print(comparison.describe())
    print()
    if comparison.arrivals_agree:
        speedup = (
            comparison.dijkstra_seconds / comparison.raptor_seconds
            if comparison.raptor_seconds
            else 0
        )
        print(
            f"  AGREE — RAPTOR {comparison.raptor_seconds * 1000:.2f} ms, "
            f"Dijkstra {comparison.dijkstra_seconds * 1000:.1f} ms ({speedup:.0f}x)"
        )
        if not comparison.trips_agree:
            print("  (different trips, same arrival — a tie broken differently)")
        return 0

    print("  DISAGREE on arrival time")
    return 1


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

    attachment: PlaceAttachment | None = None
    try:
        origin_place = _resolve_place(args.from_place, args.from_latlon)
        destination_place = _resolve_place(args.to_place, args.to_latlon)
    except GeocodingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if (origin_place is None) != (destination_place is None):
        # Mixing a stop and a place is perfectly meaningful, and the synthetic
        # stop machinery handles it — but only one end is a place, so the other
        # needs its own coordinates to attach against.
        print(
            "error: give both ends as places, or both as stops "
            "(a stop's coordinates are not yet accepted as a place)",
            file=sys.stderr,
        )
        return 2

    if origin_place is not None and destination_place is not None:
        attachment = attach_places(engine, origin_place, destination_place)
        if not attachment.is_routable:
            print(
                f"error: nothing within {int(ACCESS_MAX_METRES)} m of "
                f"{'the origin' if not attachment.origin_stops else 'the destination'}",
                file=sys.stderr,
            )
            return 1
        origin, destination = attachment.origin, attachment.destination
        if args.verbose:
            print(f"  {origin_place.name}")
            for nearby in attachment.origin_stops[:3]:
                print(f"    {nearby.metres:5.0f} m to {nearby.name}")
            print(f"  {destination_place.name}")
            for nearby in attachment.destination_stops[:3]:
                print(f"    {nearby.metres:5.0f} m to {nearby.name}")
            print()
    else:
        try:
            origin = resolve_stop(engine, args.origin)
            destination = resolve_stop(engine, args.destination)
        except StopReferenceError as exc:
            print(f"error: {exc}", file=sys.stderr)
            print("hint: find a stop with --search", file=sys.stderr)
            return 2

    if args.compare:
        return _compare(engine, origin, destination, departure, attachment=attachment)

    if args.algorithm == "raptor":
        return _plan_with_raptor(
            engine, origin, destination, departure, verbose=args.verbose, attachment=attachment
        )

    build_started = time.perf_counter()
    timetable = build_timetable(engine, departure.date())
    if attachment is not None:
        timetable = with_places(timetable, attachment)
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
