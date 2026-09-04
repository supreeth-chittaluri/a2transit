"""Redis as the boundary between the poller and everything that reads it.

The poller is a separate process from the API on purpose. One poll every twenty
seconds serves any number of API workers, the agencies see one client rather
than one per worker, and an API restart does not interrupt the feed.

**Everything written here expires.** That is the most important line in the
module. Stale realtime is worse than none: a five-minute-old prediction that a
bus is on time will confidently tell a rider to run for something that left. If
the poller dies, the keys drain and every reader falls back to the schedule
within `STALE_AFTER_SECONDS` — no health check, no flag to remember to flip, no
code path that only runs during an outage.

Payloads are JSON rather than protobuf. They are small (a few hundred KB at
most), a human can read them with `redis-cli GET`, and the WebSocket layer
forwards them to the browser unchanged.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import redis

from a2transit.config import get_settings
from a2transit.db.models import AgencySource
from a2transit.realtime.feeds import (
    FeedKind,
    ServiceAlert,
    StopPrediction,
    TripPrediction,
    VehiclePosition,
)

logger = logging.getLogger(__name__)

KEY_PREFIX = "a2transit:rt"

#: How long a snapshot stays usable. Comfortably more than the poll interval so
#: one slow fetch does not blank the map, and far less than the point at which a
#: prediction becomes a lie.
STALE_AFTER_SECONDS = 120

#: Alerts change on the scale of days, so they outlive a poller restart.
ALERT_TTL_SECONDS = 900

#: Vehicle snapshots are published here for the WebSocket to fan out.
VEHICLE_CHANNEL = f"{KEY_PREFIX}:channel:vehicles"


def _key(kind: FeedKind, agency: AgencySource) -> str:
    return f"{KEY_PREFIX}:{kind.value}:{agency.value}"


def get_client() -> redis.Redis:
    return redis.from_url(get_settings().redis_url, decode_responses=True)


@contextmanager
def client_or_none() -> Iterator[redis.Redis | None]:
    """A client, or None when Redis is unreachable.

    Every caller here is on a path that must degrade to schedule-only rather
    than fail, so the connection error is handled once, here, instead of at
    each of half a dozen call sites that would each get it slightly wrong.
    """
    client: redis.Redis | None = None
    try:
        client = get_client()
        client.ping()
    except redis.RedisError as exc:
        logger.debug("redis unavailable: %s", exc)
        yield None
        return
    try:
        yield client
    finally:
        client.close()


# --------------------------------------------------------------------------
# Serialisation. Plain dicts both ways, so the WebSocket can forward what the
# store holds without a second model in between.
# --------------------------------------------------------------------------


def _encode(payload: Any) -> str:
    return json.dumps(payload, separators=(",", ":"))


def vehicle_to_dict(vehicle: VehiclePosition) -> dict[str, Any]:
    return {
        "agency": vehicle.agency.value,
        "vehicleId": vehicle.vehicle_id,
        "tripId": vehicle.trip_id,
        "routeId": vehicle.route_id,
        "lat": vehicle.lat,
        "lon": vehicle.lon,
        "bearing": vehicle.bearing,
        "speedMps": vehicle.speed_mps,
        "timestamp": vehicle.timestamp,
    }


def vehicle_from_dict(payload: dict[str, Any]) -> VehiclePosition:
    return VehiclePosition(
        agency=AgencySource(payload["agency"]),
        vehicle_id=payload["vehicleId"],
        trip_id=payload["tripId"],
        route_id=payload["routeId"],
        lat=payload["lat"],
        lon=payload["lon"],
        bearing=payload["bearing"],
        speed_mps=payload["speedMps"],
        timestamp=payload["timestamp"],
    )


def trip_to_dict(trip: TripPrediction) -> dict[str, Any]:
    return {
        "agency": trip.agency.value,
        "tripId": trip.trip_id,
        "routeId": trip.route_id,
        "canceled": trip.canceled,
        "stops": [dataclasses.asdict(stop) for stop in trip.stops],
    }


def trip_from_dict(payload: dict[str, Any]) -> TripPrediction:
    return TripPrediction(
        agency=AgencySource(payload["agency"]),
        trip_id=payload["tripId"],
        route_id=payload["routeId"],
        canceled=payload["canceled"],
        stops=tuple(StopPrediction(**stop) for stop in payload["stops"]),
    )


def alert_to_dict(alert: ServiceAlert) -> dict[str, Any]:
    payload = dataclasses.asdict(alert)
    payload["agency"] = alert.agency.value
    return payload


def alert_from_dict(payload: dict[str, Any]) -> ServiceAlert:
    payload = dict(payload)
    payload["agency"] = AgencySource(payload["agency"])
    for field in ("route_ids", "stop_ids", "trip_ids"):
        payload[field] = tuple(payload[field])
    return ServiceAlert(**payload)


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------


def store_vehicles(
    client: redis.Redis,
    agency: AgencySource,
    vehicles: tuple[VehiclePosition, ...],
    feed_timestamp: int,
) -> None:
    client.set(
        _key(FeedKind.VEHICLES, agency),
        _encode(
            {
                "timestamp": feed_timestamp,
                "vehicles": [vehicle_to_dict(vehicle) for vehicle in vehicles],
            }
        ),
        ex=STALE_AFTER_SECONDS,
    )


def store_trips(
    client: redis.Redis,
    agency: AgencySource,
    trips: tuple[TripPrediction, ...],
    feed_timestamp: int,
) -> None:
    client.set(
        _key(FeedKind.TRIPS, agency),
        _encode(
            {"timestamp": feed_timestamp, "trips": [trip_to_dict(trip) for trip in trips]}
        ),
        ex=STALE_AFTER_SECONDS,
    )


def store_alerts(
    client: redis.Redis,
    agency: AgencySource,
    alerts: tuple[ServiceAlert, ...],
    feed_timestamp: int,
) -> None:
    client.set(
        _key(FeedKind.ALERTS, agency),
        _encode(
            {"timestamp": feed_timestamp, "alerts": [alert_to_dict(a) for a in alerts]}
        ),
        ex=ALERT_TTL_SECONDS,
    )


def publish_vehicles(client: redis.Redis, vehicles: list[dict[str, Any]]) -> int:
    """Fan the merged snapshot out to connected WebSockets. Returns subscribers."""
    return client.publish(
        VEHICLE_CHANNEL,
        _encode(
            {
                "type": "vehicles",
                "sentAt": int(dt.datetime.now(dt.UTC).timestamp()),
                "vehicles": vehicles,
            }
        ),
    )


# --------------------------------------------------------------------------
# Reading. Every one of these returns empty rather than raising.
# --------------------------------------------------------------------------


def _read(client: redis.Redis | None, kind: FeedKind, agency: AgencySource) -> dict | None:
    if client is None:
        return None
    try:
        raw = client.get(_key(kind, agency))
    except redis.RedisError as exc:
        logger.debug("redis read failed: %s", exc)
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("discarding unreadable %s payload for %s", kind.value, agency.value)
        return None


def read_vehicles(client: redis.Redis | None) -> list[dict[str, Any]]:
    vehicles: list[dict[str, Any]] = []
    for agency in AgencySource:
        payload = _read(client, FeedKind.VEHICLES, agency)
        if payload:
            vehicles.extend(payload.get("vehicles", []))
    return vehicles


def read_predictions(client: redis.Redis | None) -> tuple[TripPrediction, ...]:
    predictions: list[TripPrediction] = []
    for agency in AgencySource:
        payload = _read(client, FeedKind.TRIPS, agency)
        if not payload:
            continue
        for trip in payload.get("trips", []):
            try:
                predictions.append(trip_from_dict(trip))
            except (KeyError, TypeError, ValueError):
                logger.debug("skipping malformed prediction")
    return tuple(predictions)


def read_alerts(client: redis.Redis | None) -> tuple[ServiceAlert, ...]:
    alerts: list[ServiceAlert] = []
    for agency in AgencySource:
        payload = _read(client, FeedKind.ALERTS, agency)
        if not payload:
            continue
        for alert in payload.get("alerts", []):
            try:
                alerts.append(alert_from_dict(alert))
            except (KeyError, TypeError, ValueError):
                logger.debug("skipping malformed alert")
    return tuple(alerts)


@dataclasses.dataclass(frozen=True, slots=True)
class StoreStatus:
    """Whether realtime is usable, and how fresh it is."""

    available: bool
    ages: dict[str, int | None]

    @property
    def is_live(self) -> bool:
        return self.available and any(age is not None for age in self.ages.values())


def status(client: redis.Redis | None) -> StoreStatus:
    now = int(dt.datetime.now(dt.UTC).timestamp())
    ages: dict[str, int | None] = {}
    for agency in AgencySource:
        for kind in FeedKind:
            payload = _read(client, kind, agency)
            ages[f"{agency.value}:{kind.value}"] = (
                now - payload["timestamp"] if payload and payload.get("timestamp") else None
            )
    return StoreStatus(available=client is not None, ages=ages)


__all__ = [
    "ALERT_TTL_SECONDS",
    "STALE_AFTER_SECONDS",
    "VEHICLE_CHANNEL",
    "StoreStatus",
    "alert_from_dict",
    "alert_to_dict",
    "client_or_none",
    "get_client",
    "publish_vehicles",
    "read_alerts",
    "read_predictions",
    "read_vehicles",
    "status",
    "store_alerts",
    "store_trips",
    "store_vehicles",
    "trip_from_dict",
    "trip_to_dict",
    "vehicle_from_dict",
    "vehicle_to_dict",
]
