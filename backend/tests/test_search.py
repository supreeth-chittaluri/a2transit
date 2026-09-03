"""Earliest-arrival search, on hand-built timetables.

The real-feed itineraries live in test_itineraries.py. These cover the search's
own logic against timetables small enough to reason about completely.
"""

from __future__ import annotations

import datetime as dt

import pytest

from a2transit.db.models import AgencySource
from a2transit.routing.constants import MIN_TRANSFER_SECONDS
from a2transit.routing.graph import build_graph
from a2transit.routing.models import RideLeg, TransferLeg
from a2transit.routing.search import PlanningError, plan_on_graph
from a2transit.routing.timetable import (
    Route,
    Stop,
    Timetable,
    TransferLink,
    Trip,
    TripInstance,
    TripStop,
)

A = AgencySource.THERIDE
DAY = dt.date(2026, 9, 10)
HOUR = 3600


def _stop(stop_id: str) -> Stop:
    return Stop((A, stop_id), stop_id, A, f"Stop {stop_id}", 42.28, -83.74)


def _trip(
    trip_id: str,
    route_id: str,
    stops: list[tuple[str, int]],
    *,
    no_board_at: set[str] = frozenset(),
    no_alight_at: set[str] = frozenset(),
) -> Trip:
    """stops is [(stop_id, time)] — arrival and departure the same."""
    return Trip(
        key=(A, trip_id),
        trip_id=trip_id,
        agency=A,
        route_id=route_id,
        service_id="3",
        headsign=f"to {stops[-1][0]}",
        stops=tuple(
            TripStop(
                (A, stop_id),
                index + 1,
                time,
                time,
                can_board=stop_id not in no_board_at,
                can_alight=stop_id not in no_alight_at,
            )
            for index, (stop_id, time) in enumerate(stops)
        ),
    )


def _timetable(trips: list[Trip], transfers: tuple[TransferLink, ...] = ()) -> Timetable:
    stop_ids = {ts.stop[1] for trip in trips for ts in trip.stops}
    for link in transfers:
        stop_ids |= {link.from_stop[1], link.to_stop[1]}
    routes = {(A, trip.route_id) for trip in trips}
    return Timetable(
        base_date=DAY,
        stops={(A, sid): _stop(sid) for sid in stop_ids},
        routes={key: Route(key, key[1], f"Route {key[1]}", None) for key in routes},
        instances=tuple(TripInstance(trip, DAY, 0) for trip in trips),
        transfers=transfers,
    )


def _plan(timetable: Timetable, origin: str, destination: str, at: int, horizon: int = 6 * HOUR):
    graph = build_graph(
        timetable, start_time=at, horizon_seconds=horizon, origin=(A, origin)
    )
    return plan_on_graph(graph, timetable, (A, origin), (A, destination), at)


class TestDirectTrips:
    def test_finds_the_single_available_trip(self) -> None:
        timetable = _timetable([_trip("T1", "1", [("X", 8 * HOUR), ("Y", 8 * HOUR + 600)])])

        result = _plan(timetable, "X", "Y", 8 * HOUR)

        assert result.itinerary is not None
        assert len(result.itinerary.legs) == 1
        leg = result.itinerary.legs[0]
        assert isinstance(leg, RideLeg)
        assert leg.trip_id == "T1"
        assert leg.depart == dt.datetime(2026, 9, 10, 8, 0)
        assert leg.arrive == dt.datetime(2026, 9, 10, 8, 10)
        assert result.itinerary.transfer_count == 0

    def test_takes_the_earliest_departure_after_the_query_time(self) -> None:
        timetable = _timetable(
            [
                _trip("EARLY", "1", [("X", 7 * HOUR), ("Y", 7 * HOUR + 600)]),
                _trip("ON_TIME", "1", [("X", 9 * HOUR), ("Y", 9 * HOUR + 600)]),
                _trip("LATE", "1", [("X", 10 * HOUR), ("Y", 10 * HOUR + 600)]),
            ]
        )

        result = _plan(timetable, "X", "Y", 8 * HOUR)

        assert result.itinerary is not None
        assert result.itinerary.ride_legs[0].trip_id == "ON_TIME"

    def test_prefers_the_earlier_arrival_not_the_earlier_departure(self) -> None:
        """A later, faster bus wins."""
        timetable = _timetable(
            [
                _trip("SLOW", "1", [("X", 8 * HOUR), ("Y", 8 * HOUR + 3600)]),
                _trip("FAST", "2", [("X", 8 * HOUR + 600), ("Y", 8 * HOUR + 1200)]),
            ]
        )

        result = _plan(timetable, "X", "Y", 8 * HOUR)

        assert result.itinerary is not None
        assert result.itinerary.ride_legs[0].trip_id == "FAST"
        assert result.itinerary.arrival == dt.datetime(2026, 9, 10, 8, 20)

    def test_boarding_exactly_at_the_query_time_is_allowed(self) -> None:
        timetable = _timetable([_trip("T1", "1", [("X", 8 * HOUR), ("Y", 8 * HOUR + 600)])])

        result = _plan(timetable, "X", "Y", 8 * HOUR)

        assert result.itinerary is not None
        assert result.itinerary.initial_wait == dt.timedelta(0)

    def test_departing_a_minute_late_misses_the_bus(self) -> None:
        """Departure sensitivity — the M2 acceptance case."""
        timetable = _timetable(
            [
                _trip("FIRST", "1", [("X", 8 * HOUR), ("Y", 8 * HOUR + 600)]),
                _trip("SECOND", "1", [("X", 9 * HOUR), ("Y", 9 * HOUR + 600)]),
            ]
        )

        on_time = _plan(timetable, "X", "Y", 8 * HOUR)
        one_minute_late = _plan(timetable, "X", "Y", 8 * HOUR + 60)

        assert on_time.itinerary.ride_legs[0].trip_id == "FIRST"
        assert one_minute_late.itinerary.ride_legs[0].trip_id == "SECOND"
        assert one_minute_late.itinerary.arrival > on_time.itinerary.arrival


class TestTransfers:
    def test_same_stop_transfer_respects_the_minimum_connection_time(self) -> None:
        """A connection tighter than the floor is not usable."""
        timetable = _timetable(
            [
                _trip("IN", "1", [("X", 8 * HOUR), ("HUB", 8 * HOUR + 600)]),
                # Leaves 30 s after the first arrives — below the 60 s floor.
                _trip("TOO_TIGHT", "2", [("HUB", 8 * HOUR + 630), ("Z", 8 * HOUR + 1200)]),
                _trip("USABLE", "2", [("HUB", 8 * HOUR + 900), ("Z", 8 * HOUR + 1500)]),
            ]
        )

        result = _plan(timetable, "X", "Z", 8 * HOUR)

        assert result.itinerary is not None
        assert [leg.trip_id for leg in result.itinerary.ride_legs] == ["IN", "USABLE"]

    def test_a_connection_exactly_at_the_floor_is_usable(self) -> None:
        timetable = _timetable(
            [
                _trip("IN", "1", [("X", 8 * HOUR), ("HUB", 8 * HOUR + 600)]),
                _trip(
                    "EXACT",
                    "2",
                    [("HUB", 8 * HOUR + 600 + MIN_TRANSFER_SECONDS), ("Z", 8 * HOUR + 1200)],
                ),
            ]
        )

        result = _plan(timetable, "X", "Z", 8 * HOUR)

        assert result.itinerary is not None
        assert [leg.trip_id for leg in result.itinerary.ride_legs] == ["IN", "EXACT"]

    def test_transfer_leg_is_emitted_between_rides(self) -> None:
        timetable = _timetable(
            [
                _trip("IN", "1", [("X", 8 * HOUR), ("HUB", 8 * HOUR + 600)]),
                _trip("OUT", "2", [("HUB", 8 * HOUR + 1200), ("Z", 8 * HOUR + 1800)]),
            ]
        )

        result = _plan(timetable, "X", "Z", 8 * HOUR)

        legs = result.itinerary.legs
        assert [type(leg) for leg in legs] == [RideLeg, TransferLeg, RideLeg]
        transfer = legs[1]
        assert transfer.is_same_stop
        assert transfer.depart == dt.datetime(2026, 9, 10, 8, 10)
        assert transfer.arrive == dt.datetime(2026, 9, 10, 8, 20)
        assert result.itinerary.transfer_count == 1

    def test_declared_transfer_between_nearby_stops_is_used(self) -> None:
        link = TransferLink((A, "HUB_A"), (A, "HUB_B"), 60, 10, 29.2)
        timetable = _timetable(
            [
                _trip("IN", "1", [("X", 8 * HOUR), ("HUB_A", 8 * HOUR + 600)]),
                _trip("OUT", "2", [("HUB_B", 8 * HOUR + 1200), ("Z", 8 * HOUR + 1800)]),
            ],
            transfers=(link,),
        )

        result = _plan(timetable, "X", "Z", 8 * HOUR)

        assert result.itinerary is not None
        legs = result.itinerary.legs
        assert [leg.trip_id for leg in result.itinerary.ride_legs] == ["IN", "OUT"]
        assert not legs[1].is_same_stop
        assert legs[1].from_stop.stop_id == "HUB_A"
        assert legs[1].to_stop.stop_id == "HUB_B"

    def test_direct_trip_beats_a_faster_two_leg_option_only_on_arrival_time(self) -> None:
        """Earliest arrival is the sole criterion in M2; transfers are not penalised."""
        timetable = _timetable(
            [
                _trip("DIRECT", "1", [("X", 8 * HOUR), ("Z", 8 * HOUR + 3600)]),
                _trip("HOP1", "2", [("X", 8 * HOUR), ("HUB", 8 * HOUR + 600)]),
                _trip("HOP2", "3", [("HUB", 8 * HOUR + 900), ("Z", 8 * HOUR + 1500)]),
            ]
        )

        result = _plan(timetable, "X", "Z", 8 * HOUR)

        assert result.itinerary.arrival == dt.datetime(2026, 9, 10, 8, 25)
        assert result.itinerary.transfer_count == 1


class TestNoItinerary:
    def test_unreachable_destination_returns_none_not_an_error(self) -> None:
        timetable = _timetable(
            [
                _trip("T1", "1", [("X", 8 * HOUR), ("Y", 8 * HOUR + 600)]),
                _trip("T2", "2", [("P", 8 * HOUR), ("Q", 8 * HOUR + 600)]),
            ]
        )

        result = _plan(timetable, "X", "Q", 8 * HOUR)

        assert result.itinerary is None

    def test_query_after_the_last_departure_returns_none(self) -> None:
        timetable = _timetable([_trip("T1", "1", [("X", 8 * HOUR), ("Y", 8 * HOUR + 600)])])

        result = _plan(timetable, "X", "Y", 20 * HOUR)

        assert result.itinerary is None

    def test_a_departure_beyond_the_horizon_cannot_be_boarded(self) -> None:
        timetable = _timetable([_trip("T1", "1", [("X", 12 * HOUR), ("Y", 13 * HOUR)])])

        result = _plan(timetable, "X", "Y", 8 * HOUR, horizon=HOUR)

        assert result.itinerary is None

    def test_a_ride_boarded_inside_the_horizon_may_finish_outside_it(self) -> None:
        """The horizon bounds boardings, not arrivals.

        Cutting a trip off mid-ride would report "unreachable" for a bus the
        rider is already sitting on, so a boarded trip keeps its whole sequence.
        """
        timetable = _timetable([_trip("T1", "1", [("X", 8 * HOUR), ("Y", 20 * HOUR)])])

        result = _plan(timetable, "X", "Y", 8 * HOUR, horizon=HOUR)

        assert result.itinerary is not None
        assert result.itinerary.arrival == dt.datetime(2026, 9, 10, 20, 0)

    def test_unknown_stop_raises(self) -> None:
        timetable = _timetable([_trip("T1", "1", [("X", 8 * HOUR), ("Y", 8 * HOUR + 600)])])
        graph = build_graph(timetable, start_time=8 * HOUR, horizon_seconds=HOUR)

        with pytest.raises(PlanningError, match="unknown origin"):
            plan_on_graph(graph, timetable, (A, "NOPE"), (A, "Y"), 8 * HOUR)

    def test_same_origin_and_destination_is_a_zero_leg_itinerary(self) -> None:
        timetable = _timetable([_trip("T1", "1", [("X", 8 * HOUR), ("Y", 8 * HOUR + 600)])])

        result = _plan(timetable, "X", "X", 8 * HOUR)

        assert result.itinerary is not None
        assert result.itinerary.legs == ()
        assert result.itinerary.duration == dt.timedelta(0)


class TestBoardingRules:
    def test_cannot_board_where_pickup_is_forbidden(self) -> None:
        timetable = _timetable(
            [
                _trip(
                    "T1", "1", [("X", 8 * HOUR), ("Y", 8 * HOUR + 600)], no_board_at={"X"}
                )
            ]
        )

        result = _plan(timetable, "X", "Y", 8 * HOUR)

        assert result.itinerary is None

    def test_cannot_alight_where_drop_off_is_forbidden(self) -> None:
        timetable = _timetable(
            [
                _trip(
                    "T1", "1", [("X", 8 * HOUR), ("Y", 8 * HOUR + 600)], no_alight_at={"Y"}
                )
            ]
        )

        result = _plan(timetable, "X", "Y", 8 * HOUR)

        assert result.itinerary is None

    def test_a_no_alight_stop_can_still_be_ridden_through(self) -> None:
        """TheRide marks drop-off-only and pickup-only stops mid-route."""
        timetable = _timetable(
            [
                _trip(
                    "T1",
                    "1",
                    [("X", 8 * HOUR), ("MID", 8 * HOUR + 300), ("Y", 8 * HOUR + 600)],
                    no_alight_at={"MID"},
                )
            ]
        )

        assert _plan(timetable, "X", "MID", 8 * HOUR).itinerary is None
        assert _plan(timetable, "X", "Y", 8 * HOUR).itinerary is not None


class TestPostMidnight:
    def test_a_trip_running_past_midnight_arrives_on_the_next_day(self) -> None:
        """27:15 is 03:15 tomorrow, and must not wrap to 03:15 today."""
        timetable = _timetable(
            [_trip("NIGHT", "1", [("X", 23 * HOUR + 3000), ("Y", 27 * HOUR + 900)])]
        )

        result = _plan(timetable, "X", "Y", 23 * HOUR, horizon=6 * HOUR)

        assert result.itinerary is not None
        assert result.itinerary.departure == dt.datetime(2026, 9, 10, 23, 50)
        assert result.itinerary.arrival == dt.datetime(2026, 9, 11, 3, 15)
        assert result.itinerary.duration == dt.timedelta(hours=4, minutes=15)

    def test_a_query_after_midnight_can_use_the_previous_service_day(self) -> None:
        """The rider's clock says 00:30; the bus belongs to yesterday's service."""
        yesterday = _trip("NIGHT", "1", [("X", 24 * HOUR + 1800), ("Y", 25 * HOUR)])
        timetable = Timetable(
            base_date=DAY,
            stops={(A, "X"): _stop("X"), (A, "Y"): _stop("Y")},
            routes={(A, "1"): Route((A, "1"), "1", "Route 1", None)},
            # offset 0 means these times are already relative to DAY midnight,
            # i.e. this instance belongs to service date DAY but runs into DAY+1.
            instances=(TripInstance(yesterday, DAY, 0),),
        )

        result = _plan(timetable, "X", "Y", 24 * HOUR + 600, horizon=3 * HOUR)

        assert result.itinerary is not None
        assert result.itinerary.departure == dt.datetime(2026, 9, 11, 0, 30)
        assert result.itinerary.arrival == dt.datetime(2026, 9, 11, 1, 0)


class TestItineraryConsistency:
    def test_legs_chain_end_to_end_and_never_go_backwards(self) -> None:
        timetable = _timetable(
            [
                _trip("IN", "1", [("X", 8 * HOUR), ("HUB", 8 * HOUR + 600)]),
                _trip("OUT", "2", [("HUB", 8 * HOUR + 1200), ("Z", 8 * HOUR + 1800)]),
            ]
        )

        itinerary = _plan(timetable, "X", "Z", 8 * HOUR).itinerary

        assert itinerary.legs[0].from_stop.key == itinerary.origin.key
        assert itinerary.legs[-1].to_stop.key == itinerary.destination.key
        for earlier, later in zip(itinerary.legs, itinerary.legs[1:], strict=False):
            assert earlier.to_stop.key == later.from_stop.key
            assert earlier.arrive == later.depart
        for leg in itinerary.legs:
            assert leg.arrive >= leg.depart

    def test_intermediate_stop_count_is_reported(self) -> None:
        timetable = _timetable(
            [
                _trip(
                    "T1",
                    "1",
                    [
                        ("X", 8 * HOUR),
                        ("M1", 8 * HOUR + 120),
                        ("M2", 8 * HOUR + 240),
                        ("Y", 8 * HOUR + 360),
                    ],
                )
            ]
        )

        itinerary = _plan(timetable, "X", "Y", 8 * HOUR).itinerary

        assert itinerary.ride_legs[0].intermediate_stops == 2
