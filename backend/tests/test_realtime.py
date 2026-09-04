"""M7: GTFS-Realtime, from protobuf to a moved arrival time.

The acceptance criterion — a delay visibly moves an itinerary — is asserted with
an injected prediction rather than by waiting for a bus to run late, but it goes
through the real path: the same parser, the same matching, the same code that
patches the timetable. Nothing downstream can tell it from a live one.

Protobuf fixtures are built in-process for the same reason the GTFS ones are:
the property under test — a delay, a cancellation, a vehicle with no trip — is
visible in the test that relies on it rather than buried in a committed binary.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest
from google.transit import gtfs_realtime_pb2 as gtfs_rt
from sqlalchemy import Engine, text

from a2transit.db.models import AgencySource
from a2transit.realtime.delays import MAX_MATCH_DRIFT_SECONDS, apply_predictions
from a2transit.realtime.feeds import (
    AGENCY_TIMEZONE,
    FeedKind,
    StopPrediction,
    TripPrediction,
    parse_feed,
    service_seconds,
)
from a2transit.realtime.store import (
    alert_from_dict,
    alert_to_dict,
    trip_from_dict,
    trip_to_dict,
    vehicle_from_dict,
    vehicle_to_dict,
)
from a2transit.routing.engine import plan_with_raptor
from a2transit.routing.patterns import RaptorTimetable, build_raptor_timetable
from tests.conftest import load_real_feeds

THERIDE = AgencySource.THERIDE
MBUS = AgencySource.MBUS

#: An ordinary weekday in the feeds' service window.
THURSDAY = dt.date(2026, 9, 10)

#: Route 4's first weekday run out of YTC Stop 2, pinned since M1.
ROUTE_4_TRIP = "3572020"
ORIGIN = (THERIDE, "1338")
DESTINATION = (THERIDE, "1605")


def _epoch(day: dt.date, seconds: int) -> int:
    """Service seconds back to epoch, the inverse of feeds.service_seconds."""
    midnight = dt.datetime.combine(day, dt.time(), tzinfo=AGENCY_TIMEZONE)
    return int((midnight + dt.timedelta(seconds=seconds)).timestamp())


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def _vehicle_feed(*, with_trip: bool = True, bearing: float | None = 180.0) -> bytes:
    message = gtfs_rt.FeedMessage()
    message.header.gtfs_realtime_version = "2.0"
    message.header.timestamp = 1_788_483_928
    entity = message.entity.add()
    entity.id = "1"
    entity.vehicle.vehicle.id = "490"
    if with_trip:
        entity.vehicle.trip.trip_id = "2696020"
        entity.vehicle.trip.route_id = "22"
    entity.vehicle.position.latitude = 42.2806549
    entity.vehicle.position.longitude = -83.7460938
    if bearing is not None:
        entity.vehicle.position.bearing = bearing
    entity.vehicle.timestamp = 1_788_483_898
    return message.SerializeToString()


class TestParsingVehicles:
    def test_reads_a_position(self) -> None:
        snapshot = parse_feed(THERIDE, FeedKind.VEHICLES, _vehicle_feed())

        assert len(snapshot.vehicles) == 1
        vehicle = snapshot.vehicles[0]
        assert vehicle.vehicle_id == "490"
        assert vehicle.trip_id == "2696020"
        assert (round(vehicle.lat, 4), round(vehicle.lon, 4)) == (42.2807, -83.7461)
        assert vehicle.agency is THERIDE

    def test_an_untripped_vehicle_still_has_a_position(self) -> None:
        """A bus deadheading reports where it is and nothing else."""
        snapshot = parse_feed(THERIDE, FeedKind.VEHICLES, _vehicle_feed(with_trip=False))

        vehicle = snapshot.vehicles[0]
        assert vehicle.trip_id is None
        assert not vehicle.is_on_a_trip
        assert vehicle.lat != 0

    def test_a_missing_bearing_is_none_not_zero(self) -> None:
        """Zero is due north, which is a very different claim from "unknown"."""
        snapshot = parse_feed(THERIDE, FeedKind.VEHICLES, _vehicle_feed(bearing=None))

        assert snapshot.vehicles[0].bearing is None

    def test_the_vehicles_own_id_is_used_not_the_entity_id(self) -> None:
        """Entity ids are a per-message counter in both feeds — "1", "2", "3" —
        so keying on them would make every bus a different bus each poll."""
        snapshot = parse_feed(THERIDE, FeedKind.VEHICLES, _vehicle_feed())

        assert snapshot.vehicles[0].vehicle_id == "490"


def _trip_feed(
    trip_id: str,
    stops: list[tuple[str, int]],
    *,
    canceled: bool = False,
    skipped_stop: str | None = None,
) -> bytes:
    message = gtfs_rt.FeedMessage()
    message.header.gtfs_realtime_version = "2.0"
    message.header.timestamp = 1_788_483_928
    entity = message.entity.add()
    entity.id = "1"
    entity.trip_update.trip.trip_id = trip_id
    if canceled:
        entity.trip_update.trip.schedule_relationship = (
            gtfs_rt.TripDescriptor.ScheduleRelationship.CANCELED
        )
    for index, (stop_id, epoch) in enumerate(stops, start=1):
        update = entity.trip_update.stop_time_update.add()
        update.stop_sequence = index
        update.stop_id = stop_id
        update.arrival.time = epoch
        if stop_id == skipped_stop:
            update.schedule_relationship = (
                gtfs_rt.TripUpdate.StopTimeUpdate.ScheduleRelationship.SKIPPED
            )
    return message.SerializeToString()


class TestParsingTripUpdates:
    def test_reads_absolute_times_not_delays(self) -> None:
        """Neither agency sends `delay`; both send predicted `time`.

        This is the fact the whole delay pipeline is shaped around — a delay
        does not exist until something compares the prediction to a schedule.
        """
        payload = _trip_feed("T1", [("A", 1_788_400_000), ("B", 1_788_400_600)])

        snapshot = parse_feed(THERIDE, FeedKind.TRIPS, payload)

        prediction = snapshot.trips[0]
        assert prediction.trip_id == "T1"
        assert [stop.arrival for stop in prediction.stops] == [1_788_400_000, 1_788_400_600]
        assert prediction.first_time == 1_788_400_000

    def test_a_cancellation_is_marked(self) -> None:
        payload = _trip_feed("T1", [("A", 1_788_400_000)], canceled=True)

        assert parse_feed(THERIDE, FeedKind.TRIPS, payload).trips[0].canceled

    def test_a_skipped_stop_is_marked(self) -> None:
        payload = _trip_feed(
            "T1", [("A", 1_788_400_000), ("B", 1_788_400_600)], skipped_stop="B"
        )

        stops = parse_feed(THERIDE, FeedKind.TRIPS, payload).trips[0].stops
        assert [stop.skipped for stop in stops] == [False, True]


class TestParsingAlerts:
    def _alert_feed(self) -> bytes:
        message = gtfs_rt.FeedMessage()
        message.header.gtfs_realtime_version = "2.0"
        message.header.timestamp = 1_788_483_928
        entity = message.entity.add()
        entity.id = "btc-closure"
        alert = entity.alert
        alert.effect = gtfs_rt.Alert.Effect.DETOUR
        for language, text_value in (("es", "Cierre"), ("en", "BTC closed"), ("pt", "Fechado")):
            translation = alert.header_text.translation.add()
            translation.language = language
            translation.text = text_value
        informed = alert.informed_entity.add()
        informed.route_id = "4"
        return message.SerializeToString()

    def test_prefers_english_whatever_the_order(self) -> None:
        """TheRide ships es, en and pt with identical text and es first.

        Picking by position would make the alert's language depend on the order
        the agency happened to serialise it in, and change under the reader
        without the alert having changed.
        """
        snapshot = parse_feed(THERIDE, FeedKind.ALERTS, self._alert_feed())

        assert snapshot.alerts[0].header == "BTC closed"

    def test_carries_the_informed_routes(self) -> None:
        alert = parse_feed(THERIDE, FeedKind.ALERTS, self._alert_feed()).alerts[0]

        assert alert.route_ids == ("4",)
        assert alert.effect == "DETOUR"


class TestServiceSeconds:
    def test_round_trips_through_the_agency_timezone(self) -> None:
        """Not the machine's. A server in UTC would otherwise route yesterday."""
        assert service_seconds(_epoch(THURSDAY, 9 * 3600), THURSDAY) == 9 * 3600

    def test_past_midnight_exceeds_a_day(self) -> None:
        """The representation the whole router depends on: 27:15 stays 27:15."""
        assert service_seconds(_epoch(THURSDAY, 98_100), THURSDAY) == 98_100

    def test_before_the_base_date_goes_negative(self) -> None:
        assert service_seconds(_epoch(THURSDAY, -3600), THURSDAY) == -3600


class TestSerialisationRoundTrips:
    """Redis holds JSON, and the WebSocket forwards it to the browser unchanged."""

    def test_a_vehicle_survives(self) -> None:
        original = parse_feed(THERIDE, FeedKind.VEHICLES, _vehicle_feed()).vehicles[0]

        assert vehicle_from_dict(vehicle_to_dict(original)) == original

    def test_a_prediction_survives(self) -> None:
        payload = _trip_feed("T1", [("A", 1_788_400_000), ("B", 1_788_400_600)])
        original = parse_feed(MBUS, FeedKind.TRIPS, payload).trips[0]

        assert trip_from_dict(trip_to_dict(original)) == original

    def test_an_alert_survives(self) -> None:
        original = parse_feed(
            THERIDE, FeedKind.ALERTS, TestParsingAlerts()._alert_feed()
        ).alerts[0]

        assert alert_from_dict(alert_to_dict(original)) == original


# --------------------------------------------------------------------------
# Applying delays to the timetable
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def engine(db_engine: Engine) -> Engine:
    load_real_feeds(db_engine, patterns=True)
    return db_engine


@pytest.fixture(scope="module")
def timetable(engine: Engine) -> RaptorTimetable:
    return build_raptor_timetable(engine, THURSDAY)


@pytest.fixture(scope="module")
def route_4_schedule(engine: Engine) -> list[tuple[str, int]]:
    """(stop_id, scheduled arrival) down route 4's first weekday run."""
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT stop_id, arrival_time FROM stop_times "
                "WHERE agency_source = 'theride' AND trip_id = :trip "
                "ORDER BY stop_sequence"
            ),
            {"trip": ROUTE_4_TRIP},
        ).all()
    return [(row.stop_id, row.arrival_time) for row in rows]


def _delay_prediction(
    schedule: list[tuple[str, int]], seconds: int, *, day: dt.date = THURSDAY
) -> TripPrediction:
    return TripPrediction(
        agency=THERIDE,
        trip_id=ROUTE_4_TRIP,
        route_id="4",
        canceled=False,
        stops=tuple(
            StopPrediction(
                stop_sequence=index + 1,
                stop_id=stop_id,
                arrival=_epoch(day, scheduled + seconds),
                departure=None,
                skipped=False,
            )
            for index, (stop_id, scheduled) in enumerate(schedule)
        ),
    )


@pytest.mark.db
class TestApplyingPredictions:
    def test_nothing_to_apply_returns_the_same_object(
        self, timetable: RaptorTimetable
    ) -> None:
        """No predictions must cost nothing — this is the common case."""
        patched, report = apply_predictions(timetable, ())

        assert patched is timetable
        assert report.runs_adjusted == 0

    def test_an_on_time_prediction_changes_nothing(
        self, timetable: RaptorTimetable, route_4_schedule: list[tuple[str, int]]
    ) -> None:
        """A bus running exactly to schedule is the null case, and must be."""
        patched, report = apply_predictions(
            timetable, (_delay_prediction(route_4_schedule, 0),)
        )

        assert report.trips_matched == 1
        assert report.runs_adjusted == 0
        assert patched is timetable

    def test_a_delay_shifts_the_run(
        self, timetable: RaptorTimetable, route_4_schedule: list[tuple[str, int]]
    ) -> None:
        patched, report = apply_predictions(
            timetable, (_delay_prediction(route_4_schedule, 900),)
        )

        assert report.trips_matched == 1
        assert report.runs_adjusted == 1
        assert report.max_delay_seconds == 900

        before = _find_run(timetable, ROUTE_4_TRIP)
        after = _find_run(patched, ROUTE_4_TRIP)
        assert after.departures[0] == before.departures[0] + 900
        assert after.arrivals[-1] == before.arrivals[-1] + 900

    def test_running_early_is_applied_too(
        self, timetable: RaptorTimetable, route_4_schedule: list[tuple[str, int]]
    ) -> None:
        """Buses do run early, and a rider who misses one because the planner
        refused to believe it is worse off than one who waits."""
        patched, _ = apply_predictions(
            timetable, (_delay_prediction(route_4_schedule, -120),)
        )

        before = _find_run(timetable, ROUTE_4_TRIP)
        after = _find_run(patched, ROUTE_4_TRIP)
        assert after.departures[0] == before.departures[0] - 120

    def test_a_cancellation_removes_the_run(
        self, timetable: RaptorTimetable, route_4_schedule: list[tuple[str, int]]
    ) -> None:
        patched, report = apply_predictions(
            timetable, (_cancellation(route_4_schedule),)
        )

        assert report.trips_canceled == 1
        with pytest.raises(StopIteration):
            _find_run(patched, ROUTE_4_TRIP)

    def test_the_schedule_timetable_is_not_mutated(
        self, timetable: RaptorTimetable, route_4_schedule: list[tuple[str, int]]
    ) -> None:
        """It is cached and shared, so an overlay that edited it in place would
        make every later query depend on when the last poll landed."""
        before = _find_run(timetable, ROUTE_4_TRIP).departures

        apply_predictions(timetable, (_delay_prediction(route_4_schedule, 1800),))

        assert _find_run(timetable, ROUTE_4_TRIP).departures == before

    def test_a_prediction_for_another_day_is_not_forced_onto_this_one(
        self, timetable: RaptorTimetable, route_4_schedule: list[tuple[str, int]]
    ) -> None:
        """trip_ids repeat across service dates and the feeds send no start_date,
        so a run is matched by proximity — with a limit, or a prediction from
        last Tuesday would land on today's bus."""
        far_away = _delay_prediction(
            route_4_schedule, MAX_MATCH_DRIFT_SECONDS + 3600, day=THURSDAY
        )

        _, report = apply_predictions(timetable, (far_away,))

        assert report.trips_matched == 0
        assert report.trips_unmatched == 1

    def test_an_unknown_trip_is_counted_not_crashed_on(
        self, timetable: RaptorTimetable
    ) -> None:
        """The feeds carry trips the loaded GTFS does not — added service, or a
        feed published since the last ingest."""
        ghost = TripPrediction(
            agency=THERIDE,
            trip_id="not-a-real-trip",
            route_id=None,
            canceled=False,
            stops=(StopPrediction(1, "1338", _epoch(THURSDAY, 6 * 3600), None, False),),
        )

        _, report = apply_predictions(timetable, (ghost,))

        assert report.trips_unmatched == 1
        assert report.trips_matched == 0

    def test_a_delay_only_applies_from_the_stop_it_was_reported_at(
        self, timetable: RaptorTimetable, route_4_schedule: list[tuple[str, int]]
    ) -> None:
        """Stops the bus has already passed keep their scheduled time; the
        delay carries forward from the first prediction, never backwards."""
        tail = route_4_schedule[10:]
        prediction = TripPrediction(
            agency=THERIDE,
            trip_id=ROUTE_4_TRIP,
            route_id="4",
            canceled=False,
            stops=tuple(
                StopPrediction(
                    stop_sequence=index + 11,
                    stop_id=stop_id,
                    arrival=_epoch(THURSDAY, scheduled + 600),
                    departure=None,
                    skipped=False,
                )
                for index, (stop_id, scheduled) in enumerate(tail)
            ),
        )

        patched, _ = apply_predictions(timetable, (prediction,))

        before = _find_run(timetable, ROUTE_4_TRIP)
        after = _find_run(patched, ROUTE_4_TRIP)
        assert after.arrivals[0] == before.arrivals[0]
        assert after.arrivals[-1] == before.arrivals[-1] + 600


def _cancellation(schedule: list[tuple[str, int]]) -> TripPrediction:
    return replace(_delay_prediction(schedule, 0), canceled=True)


def _find_run(timetable: RaptorTimetable, trip_id: str):
    return next(
        run
        for pattern in timetable.patterns
        for run in pattern.runs
        if run.trip_id == trip_id and run.service_date == THURSDAY
    )


@pytest.mark.db
class TestADelayMovesAnItinerary:
    """The M7 acceptance criterion.

    Route 4's 06:02 from YTC Stop 2 to Blake, pinned since M1 and used by the
    M2 acceptance tests. Delaying it has to change the answer, and the *way* it
    changes is the interesting part.
    """

    def _plan(self, timetable: RaptorTimetable):
        return plan_with_raptor(
            timetable, ORIGIN, DESTINATION, dt.datetime.combine(THURSDAY, dt.time(6, 0))
        ).fastest

    def test_the_schedule_answer_is_the_pinned_one(
        self, timetable: RaptorTimetable
    ) -> None:
        itinerary = self._plan(timetable)

        assert itinerary.ride_legs[0].trip_id == ROUTE_4_TRIP
        assert itinerary.arrival == dt.datetime(2026, 9, 10, 6, 43)

    def test_a_small_delay_moves_the_arrival(
        self, timetable: RaptorTimetable, route_4_schedule: list[tuple[str, int]]
    ) -> None:
        """Four minutes: still the best bus, just later."""
        patched, _ = apply_predictions(
            timetable, (_delay_prediction(route_4_schedule, 240),)
        )

        itinerary = self._plan(patched)

        assert itinerary.ride_legs[0].trip_id == ROUTE_4_TRIP
        assert itinerary.arrival == dt.datetime(2026, 9, 10, 6, 47)

    def test_a_large_delay_moves_the_rider_to_another_bus(
        self, timetable: RaptorTimetable, route_4_schedule: list[tuple[str, int]]
    ) -> None:
        """Fifteen minutes, and the next scheduled run at 06:10 wins.

        This is the part worth having. A planner that only recomputed the
        arrival time would tell the rider to wait for a bus that is no longer
        their best option; because the delay is folded into the timetable before
        the search rather than applied to its output, RAPTOR simply picks a
        different one.
        """
        patched, _ = apply_predictions(
            timetable, (_delay_prediction(route_4_schedule, 900),)
        )

        itinerary = self._plan(patched)

        assert itinerary.ride_legs[0].trip_id != ROUTE_4_TRIP
        assert itinerary.departure == dt.datetime(2026, 9, 10, 6, 10)
        assert itinerary.arrival == dt.datetime(2026, 9, 10, 6, 51)

    def test_a_cancellation_removes_the_option_entirely(
        self, timetable: RaptorTimetable, route_4_schedule: list[tuple[str, int]]
    ) -> None:
        patched, _ = apply_predictions(timetable, (_cancellation(route_4_schedule),))

        itinerary = self._plan(patched)

        assert itinerary is not None, "the next bus should still be offered"
        assert itinerary.ride_legs[0].trip_id != ROUTE_4_TRIP

    def test_delays_do_not_break_the_bisect_invariant(
        self, timetable: RaptorTimetable, route_4_schedule: list[tuple[str, int]]
    ) -> None:
        """A late bus can overtake the one behind it, which is exactly the
        property `earliest_run` may only binary-search when it holds. The flag
        is recomputed rather than inherited, so a delayed pattern is scanned."""
        patched, _ = apply_predictions(
            timetable, (_delay_prediction(route_4_schedule, 3600),)
        )

        for pattern in patched.patterns:
            if not pattern.runs:
                continue
            if pattern.sorted_columns:
                for column in pattern.departure_columns:
                    assert list(column) == sorted(column), pattern.pattern_id
