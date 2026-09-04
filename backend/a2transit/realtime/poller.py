"""The loop that keeps Redis current.

    python -m a2transit.realtime            # poll both agencies forever
    python -m a2transit.realtime --once     # one cycle, then exit
    python -m a2transit.realtime --simulate-delay theride:3572020:600

Six feeds — vehicles, trips and alerts for each agency — fetched every
`realtime_poll_seconds` (20 by default). At that rate this is about 250 kB a
minute from each agency, which is well inside what an unauthenticated public
endpoint should expect from one consumer, and is the reason the poller is a
single process rather than something each API worker does for itself.

A cycle never raises. A feed that fails is skipped and retried next time, and
its Redis key expires on its own — so an outage degrades to schedule-only
routing without anything having to notice.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import signal
import sys
import time
from dataclasses import dataclass, field
from types import FrameType

import httpx

from a2transit.config import get_settings
from a2transit.db.models import AgencySource
from a2transit.realtime import store
from a2transit.realtime.feeds import (
    FeedKind,
    TripPrediction,
    fetch_feed,
)

logger = logging.getLogger("a2transit.realtime")


@dataclass
class CycleResult:
    vehicles: int = 0
    trips: int = 0
    alerts: int = 0
    failures: list[str] = field(default_factory=list)
    subscribers: int = 0
    seconds: float = 0.0

    def __str__(self) -> str:
        failed = f", {len(self.failures)} feed(s) failed: {', '.join(self.failures)}" if (
            self.failures
        ) else ""
        return (
            f"{self.vehicles} vehicles, {self.trips} trip updates, "
            f"{self.alerts} alerts in {self.seconds * 1000:.0f} ms"
            f"{failed}"
        )


def poll_once(
    client, *, http: httpx.Client | None = None, simulated: tuple[TripPrediction, ...] = ()
) -> CycleResult:
    """One pass over all six feeds. Never raises."""
    started = time.perf_counter()
    result = CycleResult()
    owned = http is None
    http = http or httpx.Client(timeout=15.0, follow_redirects=True)

    merged_vehicles: list[dict] = []
    try:
        for agency in AgencySource:
            for kind in FeedKind:
                snapshot = fetch_feed(agency, kind, client=http)
                if snapshot is None:
                    result.failures.append(f"{agency.value}/{kind.value}")
                    continue

                if kind is FeedKind.VEHICLES:
                    store.store_vehicles(
                        client, agency, snapshot.vehicles, snapshot.timestamp
                    )
                    merged_vehicles.extend(
                        store.vehicle_to_dict(vehicle) for vehicle in snapshot.vehicles
                    )
                    result.vehicles += len(snapshot.vehicles)
                elif kind is FeedKind.TRIPS:
                    trips = snapshot.trips
                    if simulated:
                        trips = _merge_simulated(trips, simulated, agency)
                    store.store_trips(client, agency, trips, snapshot.timestamp)
                    result.trips += len(trips)
                else:
                    store.store_alerts(client, agency, snapshot.alerts, snapshot.timestamp)
                    result.alerts += len(snapshot.alerts)
    finally:
        if owned:
            http.close()

    # Published even when empty: a subscriber needs to hear "no vehicles" to
    # clear the map, rather than keep showing the last ones it saw forever.
    result.subscribers = store.publish_vehicles(client, merged_vehicles)
    result.seconds = time.perf_counter() - started
    return result


def _merge_simulated(
    real: tuple[TripPrediction, ...],
    simulated: tuple[TripPrediction, ...],
    agency: AgencySource,
) -> tuple[TripPrediction, ...]:
    """Simulated predictions replace the live one for the same trip."""
    mine = {p.trip_id: p for p in simulated if p.agency is agency}
    if not mine:
        return real
    kept = tuple(p for p in real if p.trip_id not in mine)
    return kept + tuple(mine.values())


def run_forever(interval_seconds: int, *, simulated: tuple[TripPrediction, ...] = ()) -> int:
    stopping = False

    def stop(signum: int, _frame: FrameType | None) -> None:
        nonlocal stopping
        logger.info("signal %s — finishing the current cycle", signal.Signals(signum).name)
        stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    with store.client_or_none() as client:
        if client is None:
            logger.error("Redis unreachable — is `docker compose up -d` running?")
            return 1

        with httpx.Client(timeout=15.0, follow_redirects=True) as http:
            while not stopping:
                result = poll_once(client, http=http, simulated=simulated)
                logger.info("%s", result)
                # Slept in short slices so a signal is honoured promptly rather
                # than up to a full interval later.
                deadline = time.monotonic() + interval_seconds
                while not stopping and time.monotonic() < deadline:
                    time.sleep(0.2)
    return 0


def parse_simulated_delay(spec: str) -> TripPrediction:
    """`agency:trip_id:seconds` into a prediction that delays every stop.

    The acceptance criterion for realtime is that a delay visibly moves an
    itinerary's arrival, and waiting for a bus to run late on demand is not a
    test. This injects one, through exactly the path a real prediction takes:
    it is stored in Redis, read back by the API, and applied to the timetable by
    the same code. Nothing downstream can tell the difference, which is what
    makes it worth having.
    """
    try:
        agency_name, trip_id, seconds = spec.split(":")
        agency = AgencySource(agency_name)
        delay = int(seconds)
    except ValueError:
        raise SystemExit(
            f"--simulate-delay expects agency:trip_id:seconds, got {spec!r}"
        ) from None

    from sqlalchemy import text

    from a2transit.db.session import get_engine
    from a2transit.realtime.feeds import AGENCY_TIMEZONE, current_service_date

    with get_engine().connect() as connection:
        rows = connection.execute(
            text(
                "SELECT stop_id, arrival_time FROM stop_times "
                "WHERE agency_source = :agency AND trip_id = :trip_id "
                "ORDER BY stop_sequence"
            ),
            {"agency": agency.value, "trip_id": trip_id},
        ).all()
    if not rows:
        raise SystemExit(f"no trip {agency.value}:{trip_id} in the database")

    from a2transit.realtime.feeds import StopPrediction

    midnight = dt.datetime.combine(
        current_service_date(), dt.time(), tzinfo=AGENCY_TIMEZONE
    )
    stops = tuple(
        StopPrediction(
            stop_sequence=index + 1,
            stop_id=row.stop_id,
            arrival=int((midnight + dt.timedelta(seconds=row.arrival_time + delay)).timestamp()),
            departure=None,
            skipped=False,
        )
        for index, row in enumerate(rows)
    )
    logger.info(
        "simulating %s on %s:%s across %d stops", f"+{delay}s", agency.value, trip_id, len(stops)
    )
    return TripPrediction(
        agency=agency, trip_id=trip_id, route_id=None, canceled=False, stops=stops
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        prog="python -m a2transit.realtime",
        description="Poll both agencies' GTFS-Realtime feeds into Redis.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=settings.realtime_poll_seconds,
        metavar="SECONDS",
        help=f"Seconds between cycles (default: {settings.realtime_poll_seconds}).",
    )
    parser.add_argument("--once", action="store_true", help="Poll once and exit.")
    parser.add_argument(
        "--simulate-delay",
        metavar="AGENCY:TRIP:SECONDS",
        action="append",
        default=[],
        help="Inject a delay on one trip, for demonstrating the realtime path.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    simulated = tuple(parse_simulated_delay(spec) for spec in args.simulate_delay)

    if args.once:
        with store.client_or_none() as client:
            if client is None:
                logger.error("Redis unreachable — is `docker compose up -d` running?")
                return 1
            result = poll_once(client, simulated=simulated)
            logger.info("%s", result)
            return 1 if len(result.failures) == len(AgencySource) * len(FeedKind) else 0

    return run_forever(args.interval, simulated=simulated)


if __name__ == "__main__":
    sys.exit(main())
