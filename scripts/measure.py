"""Produce every number the README quotes, in one run.

    ./backend/.venv/bin/python scripts/measure.py
    ./backend/.venv/bin/python scripts/measure.py --cases 500 --realtime

Written because a README full of measurements is a README full of numbers that
quietly stop being true. Everything here is measured against the database as it
stands, so re-running it after a feed refresh says whether the claims still
hold — and the seeded case list means the latency figures are comparable
between runs rather than sampled afresh each time.

`--realtime` additionally polls the live feeds and reports end-to-end lag, so it
needs the network and a Redis; the rest is offline once the feeds are ingested.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from a2transit.db.models import AgencySource
from a2transit.db.session import get_engine
from a2transit.realtime import store
from a2transit.realtime.delays import apply_predictions
from a2transit.realtime.feeds import (
    FeedKind,
    current_service_date,
    fetch_feed,
)
from a2transit.routing.compare import (
    compare_cases,
    generate_cases,
    load_servable_stops,
    summarise,
)
from a2transit.routing.engine import plan_with_raptor
from a2transit.routing.graph import build_graph
from a2transit.routing.patterns import build_raptor_timetable
from a2transit.routing.places import Place, attach_places, with_places_raptor
from a2transit.routing.timetable import build_timetable
from sqlalchemy import Engine, text

#: The weekday every other measurement in the project uses.
THURSDAY = dt.date(2026, 9, 10)


def rule(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(int(fraction * len(ordered)), len(ordered) - 1)]


# --------------------------------------------------------------------------


def feed_scale(engine: Engine) -> None:
    rule("Feed scale")
    tables = (
        "stops", "routes", "trips", "stop_times", "shapes", "calendar",
        "calendar_dates", "transfers", "route_patterns", "pattern_stops",
        "trips_by_pattern",
    )
    print(f"{'table':<20}{'theride':>12}{'mbus':>12}{'total':>12}")
    grand = 0
    for table in tables:
        with engine.connect() as connection:
            rows = connection.execute(
                text(f"SELECT agency_source::text AS a, count(*) AS n FROM {table} GROUP BY 1")
            ).all()
        counts = {row.a: row.n for row in rows}
        total = sum(counts.values())
        grand += total
        print(
            f"{table:<20}{counts.get('theride', 0):>12,}"
            f"{counts.get('mbus', 0):>12,}{total:>12,}"
        )

    with engine.connect() as connection:
        footpaths = connection.execute(text("SELECT count(*) FROM footpaths")).scalar()
        crossing = connection.execute(
            text(
                "SELECT count(*) FROM footpaths "
                "WHERE from_agency_source <> to_agency_source"
            )
        ).scalar()
    print(f"{'footpaths':<20}{'':>12}{'':>12}{footpaths:>12,}")
    print(f"{'  cross-agency':<20}{'':>12}{'':>12}{crossing:>12,}")
    print(f"{'TOTAL':<20}{'':>12}{'':>12}{grand + footpaths:>12,}")


def build_costs(engine: Engine) -> None:
    rule("Preprocessing and build cost")
    started = time.perf_counter()
    raptor = build_raptor_timetable(engine, THURSDAY)
    raptor_ms = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    dijkstra = build_timetable(engine, THURSDAY)
    dijkstra_ms = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    graph = build_graph(
        dijkstra,
        start_time=9 * 3600,
        origin=(AgencySource.THERIDE, "544"),
        destination=(AgencySource.THERIDE, "1019"),
    )
    graph_ms = (time.perf_counter() - started) * 1000

    print(f"RAPTOR timetable       {raptor_ms:>8.0f} ms   "
          f"{len(raptor.patterns)} patterns, {raptor.run_count:,} runs")
    print(f"Dijkstra timetable     {dijkstra_ms:>8.0f} ms   "
          f"{len(dijkstra.instances):,} trip instances")
    print(f"Time-expanded graph    {graph_ms:>8.0f} ms   "
          f"{graph.node_count:,} nodes, {graph.edge_count:,} edges")


def query_latency(engine: Engine, cases: int) -> None:
    rule(f"Query latency, {cases} seeded cases")
    stops = load_servable_stops(engine)
    comparisons = compare_cases(engine, generate_cases(stops, cases))
    summary = summarise(comparisons)
    print(summary.report())


def door_to_door_latency(engine: Engine, runs: int = 40) -> None:
    rule(f"Door-to-door latency, {runs} runs")
    timetable = build_raptor_timetable(engine, THURSDAY)
    origin = Place("Kerrytown Market", 42.2846, -83.7454)
    destination = Place("Michigan Stadium", 42.2658, -83.7478)
    when = dt.datetime.combine(THURSDAY, dt.time(9, 0))

    attach_ms: list[float] = []
    query_ms: list[float] = []
    for _ in range(runs):
        started = time.perf_counter()
        attachment = attach_places(engine, origin, destination)
        attached = with_places_raptor(timetable, attachment)
        attach_ms.append((time.perf_counter() - started) * 1000)

        outcome = plan_with_raptor(
            attached, attachment.origin, attachment.destination, when
        )
        query_ms.append(outcome.seconds * 1000)

    print(f"attach places   p50 {percentile(attach_ms, 0.5):6.2f} ms  "
          f"p95 {percentile(attach_ms, 0.95):6.2f} ms")
    print(f"RAPTOR query    p50 {percentile(query_ms, 0.5):6.2f} ms  "
          f"p95 {percentile(query_ms, 0.95):6.2f} ms")
    print(f"(Kerrytown to Michigan Stadium, {runs} runs, one PostGIS round trip each)")


@dataclass
class LagSample:
    feed: str
    #: Seconds between the feed's own header timestamp and it being usable.
    lag: float
    entities: int


def realtime_lag(engine: Engine) -> None:
    rule("Realtime end-to-end lag")
    now = time.time()
    samples: list[LagSample] = []
    for agency in AgencySource:
        for kind in FeedKind:
            started = time.perf_counter()
            snapshot = fetch_feed(agency, kind)
            fetch_seconds = time.perf_counter() - started
            if snapshot is None:
                print(f"{agency.value}/{kind.value}: unavailable")
                continue
            # The header timestamp is when the agency built the message; the
            # gap to now is publication lag plus our fetch, which is the number
            # a rider experiences.
            samples.append(
                LagSample(
                    feed=f"{agency.value}/{kind.value}",
                    lag=now - snapshot.timestamp + fetch_seconds,
                    entities=snapshot.entity_count,
                )
            )

    print(f"{'feed':<22}{'age at fetch':>14}{'entities':>10}")
    for sample in samples:
        print(f"{sample.feed:<22}{sample.lag:>12.1f} s{sample.entities:>10,}")

    predictions = []
    for agency in AgencySource:
        snapshot = fetch_feed(agency, FeedKind.TRIPS)
        if snapshot:
            predictions.extend(snapshot.trips)

    if predictions:
        timetable = build_raptor_timetable(engine, current_service_date())
        started = time.perf_counter()
        _, report = apply_predictions(timetable, tuple(predictions))
        apply_ms = (time.perf_counter() - started) * 1000
        print(f"\napply {len(predictions)} predictions   {apply_ms:.0f} ms")
        print(f"  {report}")

    with store.client_or_none() as client:
        state = store.status(client)
    print(f"\nRedis reachable: {state.available}; snapshots present: {state.is_live}")
    if state.is_live:
        ages = [age for age in state.ages.values() if age is not None]
        print(f"stored snapshot age: {min(ages)}–{max(ages)} s "
              f"(expires at {store.STALE_AFTER_SECONDS} s)")


def correctness(engine: Engine) -> None:
    rule("Correctness spot-checks")
    timetable = build_raptor_timetable(engine, THURSDAY)

    checks = [
        (
            "route 4, first weekday run",
            (AgencySource.THERIDE, "1338"),
            (AgencySource.THERIDE, "1605"),
            dt.datetime.combine(THURSDAY, dt.time(6, 0)),
        ),
        (
            "cross-agency, Blake to Central Campus",
            (AgencySource.THERIDE, "1605"),
            (AgencySource.MBUS, "207"),
            dt.datetime.combine(THURSDAY, dt.time(9, 0)),
        ),
        (
            "three-option trade-off",
            (AgencySource.THERIDE, "357"),
            (AgencySource.THERIDE, "1330"),
            dt.datetime.combine(THURSDAY, dt.time(8, 45)),
        ),
        (
            "post-midnight arrival",
            (AgencySource.MBUS, "275"),
            (AgencySource.MBUS, "207"),
            dt.datetime.combine(THURSDAY, dt.time(23, 50)),
        ),
    ]

    for label, origin, destination, when in checks:
        outcome = plan_with_raptor(timetable, origin, destination, when)
        if not outcome.itineraries:
            print(f"{label:<38} no itinerary")
            continue
        fastest = outcome.fastest
        agencies = "+".join(
            sorted({leg.agency.value for leg in fastest.ride_legs}) or ["walk"]
        )
        print(
            f"{label:<38} {len(outcome.itineraries)} option(s), "
            f"arrive {fastest.arrival:%a %H:%M:%S}, "
            f"{fastest.transfer_count} change(s), {agencies}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure the whole system.")
    parser.add_argument("--cases", type=int, default=120, help="Differential sample size.")
    parser.add_argument(
        "--realtime", action="store_true", help="Also measure the live feeds (network)."
    )
    args = parser.parse_args(argv)

    engine = get_engine()
    print(f"a2transit measurements — {dt.datetime.now():%Y-%m-%d %H:%M}")

    feed_scale(engine)
    build_costs(engine)
    correctness(engine)
    door_to_door_latency(engine)
    query_latency(engine, args.cases)
    if args.realtime:
        realtime_lag(engine)
    return 0


if __name__ == "__main__":
    sys.exit(main())
