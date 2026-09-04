"""Parsing GTFS-Realtime into plain objects.

Both agencies run the same Clever Devices stack and serve spec-compliant
GTFS-Realtime v2.0 protobuf, so one parser handles both and only the base URL
differs. What comes out here is deliberately dumb: dataclasses with epoch
timestamps and no opinion about the schedule. Everything that needs to know
what a bus was *supposed* to do lives in `realtime.delays`, which has the
timetable to compare against.

The one thing worth knowing before writing any of this: **neither feed sends
`delay`.** Both send absolute predicted `time` on each `stop_time_update`. That
is allowed — the spec offers either — but it means a delay cannot be read off
the feed, only computed against the scheduled time, which is why the two halves
are split the way they are.

Feeds are treated as best-effort throughout. They are undocumented in TheRide's
case (see docs/feeds.md) and could move or stop without notice, so every
failure mode here is "return nothing and carry on", never an exception that
reaches a rider planning a trip.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from enum import StrEnum
from zoneinfo import ZoneInfo

import httpx
from google.transit import gtfs_realtime_pb2 as gtfs_rt

from a2transit.config import get_settings
from a2transit.db.models import AgencySource

logger = logging.getLogger(__name__)

#: Both agencies operate in one timezone. The feeds' epoch timestamps have to be
#: turned into a local service time somewhere, and doing it against the machine's
#: locale would make a server in UTC route yesterday's buses.
AGENCY_TIMEZONE = ZoneInfo("America/Detroit")

#: Preferred language for alert text, then anything.
PREFERRED_LANGUAGE = "en"

DEFAULT_TIMEOUT_SECONDS = 15.0


class FeedKind(StrEnum):
    VEHICLES = "vehicles"
    TRIPS = "trips"
    ALERTS = "alerts"


@dataclass(frozen=True, slots=True)
class VehiclePosition:
    agency: AgencySource
    vehicle_id: str
    trip_id: str | None
    route_id: str | None
    lat: float
    lon: float
    bearing: float | None
    speed_mps: float | None
    #: Epoch seconds, as reported by the vehicle rather than the server.
    timestamp: int

    @property
    def key(self) -> tuple[AgencySource, str]:
        return (self.agency, self.vehicle_id)

    @property
    def is_on_a_trip(self) -> bool:
        """Not every vehicle is. A bus deadheading reports position only."""
        return self.trip_id is not None


@dataclass(frozen=True, slots=True)
class StopPrediction:
    stop_sequence: int | None
    stop_id: str | None
    #: Epoch seconds. Either may be absent; the spec allows one without the other.
    arrival: int | None
    departure: int | None
    skipped: bool

    @property
    def best_time(self) -> int | None:
        """Arrival if given, else departure. Riders board on departure but the
        feeds mostly publish arrival, and for a delay either will do."""
        return self.arrival if self.arrival is not None else self.departure


@dataclass(frozen=True, slots=True)
class TripPrediction:
    agency: AgencySource
    trip_id: str
    route_id: str | None
    canceled: bool
    stops: tuple[StopPrediction, ...]

    @property
    def key(self) -> tuple[AgencySource, str]:
        return (self.agency, self.trip_id)

    @property
    def first_time(self) -> int | None:
        for stop in self.stops:
            if stop.best_time is not None:
                return stop.best_time
        return None


@dataclass(frozen=True, slots=True)
class ServiceAlert:
    agency: AgencySource
    alert_id: str
    header: str
    description: str
    cause: str
    effect: str
    url: str | None
    route_ids: tuple[str, ...]
    stop_ids: tuple[str, ...]
    trip_ids: tuple[str, ...]
    active_from: int | None
    active_until: int | None

    def is_active_at(self, moment: int) -> bool:
        if self.active_from is not None and moment < self.active_from:
            return False
        if self.active_until is not None and moment > self.active_until:
            return False
        return True


@dataclass(frozen=True, slots=True)
class FeedSnapshot:
    agency: AgencySource
    kind: FeedKind
    #: The feed's own header timestamp, not when we fetched it.
    timestamp: int
    vehicles: tuple[VehiclePosition, ...] = ()
    trips: tuple[TripPrediction, ...] = ()
    alerts: tuple[ServiceAlert, ...] = ()

    @property
    def entity_count(self) -> int:
        return len(self.vehicles) + len(self.trips) + len(self.alerts)


def feed_url(agency: AgencySource, kind: FeedKind) -> str:
    settings = get_settings()
    return getattr(settings, f"{agency.value}_gtfsrt_{kind.value}_url")


def _translated(text_field, language: str = PREFERRED_LANGUAGE) -> str:
    """One string out of a GTFS-RT TranslatedString.

    TheRide publishes the same English sentence under `en`, `es` and `pt`, so
    picking a language matters less than picking deterministically — an alert
    whose text changes between polls looks like news when it is not.
    """
    translations = list(text_field.translation)
    if not translations:
        return ""
    for translation in translations:
        if translation.language == language:
            return translation.text
    return translations[0].text


def _parse_vehicles(agency: AgencySource, message) -> tuple[VehiclePosition, ...]:
    positions: list[VehiclePosition] = []
    for entity in message.entity:
        if not entity.HasField("vehicle"):
            continue
        vehicle = entity.vehicle
        if not vehicle.HasField("position"):
            continue
        # The vehicle's own id where it has one; the entity id is a per-message
        # counter in both feeds ("1", "2", ...) and is not stable across polls.
        vehicle_id = vehicle.vehicle.id or entity.id
        positions.append(
            VehiclePosition(
                agency=agency,
                vehicle_id=vehicle_id,
                trip_id=vehicle.trip.trip_id or None,
                route_id=vehicle.trip.route_id or None,
                lat=vehicle.position.latitude,
                lon=vehicle.position.longitude,
                bearing=vehicle.position.bearing
                if vehicle.position.HasField("bearing")
                else None,
                speed_mps=vehicle.position.speed
                if vehicle.position.HasField("speed")
                else None,
                timestamp=vehicle.timestamp or message.header.timestamp,
            )
        )
    return tuple(positions)


def _parse_trips(agency: AgencySource, message) -> tuple[TripPrediction, ...]:
    predictions: list[TripPrediction] = []
    for entity in message.entity:
        if not entity.HasField("trip_update"):
            continue
        update = entity.trip_update
        trip_id = update.trip.trip_id
        if not trip_id:
            continue

        canceled = (
            update.trip.schedule_relationship
            == gtfs_rt.TripDescriptor.ScheduleRelationship.CANCELED
        )
        stops = tuple(
            StopPrediction(
                stop_sequence=stop.stop_sequence if stop.HasField("stop_sequence") else None,
                stop_id=stop.stop_id or None,
                arrival=stop.arrival.time if stop.HasField("arrival") else None,
                departure=stop.departure.time if stop.HasField("departure") else None,
                skipped=(
                    stop.schedule_relationship
                    == gtfs_rt.TripUpdate.StopTimeUpdate.ScheduleRelationship.SKIPPED
                ),
            )
            for stop in update.stop_time_update
        )
        predictions.append(
            TripPrediction(
                agency=agency,
                trip_id=trip_id,
                route_id=update.trip.route_id or None,
                canceled=canceled,
                stops=stops,
            )
        )
    return tuple(predictions)


def _parse_alerts(agency: AgencySource, message) -> tuple[ServiceAlert, ...]:
    alerts: list[ServiceAlert] = []
    for entity in message.entity:
        if not entity.HasField("alert"):
            continue
        alert = entity.alert
        periods = list(alert.active_period)
        alerts.append(
            ServiceAlert(
                agency=agency,
                alert_id=entity.id,
                header=_translated(alert.header_text),
                description=_translated(alert.description_text),
                cause=gtfs_rt.Alert.Cause.Name(alert.cause),
                effect=gtfs_rt.Alert.Effect.Name(alert.effect),
                url=_translated(alert.url) or None,
                route_ids=tuple(
                    informed.route_id for informed in alert.informed_entity if informed.route_id
                ),
                stop_ids=tuple(
                    informed.stop_id for informed in alert.informed_entity if informed.stop_id
                ),
                trip_ids=tuple(
                    informed.trip.trip_id
                    for informed in alert.informed_entity
                    if informed.trip.trip_id
                ),
                active_from=periods[0].start if periods and periods[0].start else None,
                active_until=periods[0].end if periods and periods[0].end else None,
            )
        )
    return tuple(alerts)


def parse_feed(agency: AgencySource, kind: FeedKind, payload: bytes) -> FeedSnapshot:
    """Protobuf bytes to a snapshot. Raises only on genuinely unparseable input."""
    message = gtfs_rt.FeedMessage()
    message.ParseFromString(payload)

    timestamp = message.header.timestamp or int(dt.datetime.now(dt.UTC).timestamp())
    if kind is FeedKind.VEHICLES:
        return FeedSnapshot(agency, kind, timestamp, vehicles=_parse_vehicles(agency, message))
    if kind is FeedKind.TRIPS:
        return FeedSnapshot(agency, kind, timestamp, trips=_parse_trips(agency, message))
    return FeedSnapshot(agency, kind, timestamp, alerts=_parse_alerts(agency, message))


def fetch_feed(
    agency: AgencySource,
    kind: FeedKind,
    *,
    client: httpx.Client | None = None,
) -> FeedSnapshot | None:
    """One feed, or None if it could not be had.

    None rather than an exception: realtime is an enhancement, and a poller that
    dies because somebody's undocumented endpoint returned a 502 takes the live
    map down with it. The caller logs and tries again in twenty seconds.
    """
    owned = client is None
    client = client or httpx.Client(timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=True)
    try:
        response = client.get(feed_url(agency, kind))
        response.raise_for_status()
        return parse_feed(agency, kind, response.content)
    except httpx.HTTPError as exc:
        logger.warning("%s %s: %s", agency.value, kind.value, exc)
        return None
    except Exception:
        # A malformed protobuf is the same problem as an unreachable host, from
        # the poller's point of view, and is worth a stack trace exactly once.
        logger.exception("%s %s: unparseable payload", agency.value, kind.value)
        return None
    finally:
        if owned:
            client.close()


def service_seconds(epoch: int, base_date: dt.date) -> int:
    """Epoch seconds to the router's own clock: seconds since base_date midnight.

    Local midnight, in the agencies' timezone, because that is what the router's
    integer times count from. Values below zero and above 86,400 are expected
    and correct — that is the whole reason the router uses this representation.
    """
    moment = dt.datetime.fromtimestamp(epoch, AGENCY_TIMEZONE)
    midnight = dt.datetime.combine(base_date, dt.time(), tzinfo=AGENCY_TIMEZONE)
    return int((moment - midnight).total_seconds())


def current_service_date(epoch: int | None = None) -> dt.date:
    """The calendar date, locally, of an epoch timestamp."""
    epoch = epoch if epoch is not None else int(dt.datetime.now(dt.UTC).timestamp())
    return dt.datetime.fromtimestamp(epoch, AGENCY_TIMEZONE).date()


__all__ = [
    "AGENCY_TIMEZONE",
    "FeedKind",
    "FeedSnapshot",
    "ServiceAlert",
    "StopPrediction",
    "TripPrediction",
    "VehiclePosition",
    "current_service_date",
    "feed_url",
    "fetch_feed",
    "parse_feed",
    "service_seconds",
]
