"""Timetable assembly: the service-day window, offsets, and transfer times."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import Engine

from a2transit.db.models import AgencySource
from a2transit.routing.constants import MIN_TRANSFER_SECONDS, SECONDS_PER_DAY
from a2transit.routing.timetable import (
    Timetable,
    build_timetable,
    load_footpaths,
    service_date_window,
)
from tests.conftest import load_real_feeds

THURSDAY = dt.date(2026, 9, 10)
LABOR_DAY = dt.date(2026, 9, 7)


class TestServiceDateWindow:
    def test_spans_the_day_before_and_after(self) -> None:
        window = service_date_window(THURSDAY)

        assert window == (
            (dt.date(2026, 9, 9), -SECONDS_PER_DAY),
            (dt.date(2026, 9, 10), 0),
            (dt.date(2026, 9, 11), SECONDS_PER_DAY),
        )

    def test_crosses_a_month_boundary(self) -> None:
        window = service_date_window(dt.date(2026, 10, 1))

        assert [day for day, _ in window] == [
            dt.date(2026, 9, 30),
            dt.date(2026, 10, 1),
            dt.date(2026, 10, 2),
        ]


class TestAbsoluteTime:
    @pytest.fixture
    def timetable(self) -> Timetable:
        return Timetable(base_date=THURSDAY, stops={}, routes={}, instances=())

    def test_ordinary_time(self, timetable: Timetable) -> None:
        assert timetable.absolute_time(6 * 3600 + 2 * 60) == dt.datetime(2026, 9, 10, 6, 2)

    def test_time_past_midnight_lands_on_the_next_calendar_day(
        self, timetable: Timetable
    ) -> None:
        """27:15:00 on the query date is 03:15 the following morning."""
        assert timetable.absolute_time(98_100) == dt.datetime(2026, 9, 11, 3, 15)

    def test_negative_time_lands_on_the_previous_calendar_day(
        self, timetable: Timetable
    ) -> None:
        """A trip from service date D-1 that has not yet reached the query date."""
        assert timetable.absolute_time(-3600) == dt.datetime(2026, 9, 9, 23, 0)


@pytest.fixture(scope="module")
def loaded_engine(db_engine: Engine) -> Engine:
    return load_real_feeds(db_engine)


@pytest.fixture(scope="module")
def thursday_timetable(loaded_engine: Engine) -> Timetable:
    return build_timetable(loaded_engine, THURSDAY)


@pytest.mark.db
class TestBuildTimetable:
    def test_instances_cover_all_three_service_dates(
        self, thursday_timetable: Timetable
    ) -> None:
        dates = {instance.service_date for instance in thursday_timetable.instances}

        assert dates == {dt.date(2026, 9, 9), dt.date(2026, 9, 10), dt.date(2026, 9, 11)}

    def test_instance_counts_match_the_resolved_calendar(
        self, thursday_timetable: Timetable
    ) -> None:
        """TheRide runs 2,063 weekday trips; MBus 1,668 Thu and 1,676 Fri."""
        counts: dict[tuple[str, dt.date], int] = {}
        for instance in thursday_timetable.instances:
            key = (instance.trip.agency.value, instance.service_date)
            counts[key] = counts.get(key, 0) + 1

        assert counts[("theride", dt.date(2026, 9, 10))] == 2063
        assert counts[("mbus", dt.date(2026, 9, 10))] == 1668
        assert counts[("mbus", dt.date(2026, 9, 11))] == 1676

    def test_same_trip_on_two_dates_shares_one_underlying_trip_object(
        self, thursday_timetable: Timetable
    ) -> None:
        """Stop sequences are loaded once, not copied per date."""
        by_trip: dict[str, list] = {}
        for instance in thursday_timetable.instances:
            if instance.trip.agency is AgencySource.THERIDE:
                by_trip.setdefault(instance.trip.trip_id, []).append(instance)

        repeated = next(group for group in by_trip.values() if len(group) > 1)
        first, second = repeated[0], repeated[1]

        assert first.service_date != second.service_date
        assert first.trip is second.trip
        assert first.offset != second.offset

    def test_offsets_separate_instances_of_the_same_trip(
        self, thursday_timetable: Timetable
    ) -> None:
        by_trip: dict[str, list] = {}
        for instance in thursday_timetable.instances:
            by_trip.setdefault(instance.trip.trip_id, []).append(instance)

        group = sorted(
            next(g for g in by_trip.values() if len(g) >= 3), key=lambda i: i.service_date
        )
        departures = [instance.departure_at(0) for instance in group[:3]]

        assert departures[1] - departures[0] == SECONDS_PER_DAY
        assert departures[2] - departures[1] == SECONDS_PER_DAY

    def test_post_midnight_arrival_resolves_to_the_following_day(
        self, thursday_timetable: Timetable
    ) -> None:
        latest = max(
            thursday_timetable.instances,
            key=lambda i: i.arrival_at(len(i.trip.stops) - 1),
        )
        arrival = thursday_timetable.absolute_time(latest.arrival_at(len(latest.trip.stops) - 1))

        # The last MBus service date in the window is Fri 2026-09-11, and its
        # latest trip reaches 27:15 — 03:15 on Saturday.
        assert arrival == dt.datetime(2026, 9, 12, 3, 15)

    def test_labor_day_timetable_is_much_smaller(self, loaded_engine: Engine) -> None:
        holiday = build_timetable(loaded_engine, LABOR_DAY)

        mbus_monday = [
            instance
            for instance in holiday.instances
            if instance.trip.agency is AgencySource.MBUS
            and instance.service_date == LABOR_DAY
        ]

        assert len(mbus_monday) == 366


@pytest.mark.db
class TestFootpaths:
    """What the timetable loads from the M4 footpath table.

    The point of loading them here rather than deriving a transfer set from
    transfers.txt is that RAPTOR reads exactly the same rows. Before M4 each
    engine built its own set and they agreed only because a transitive closure
    forced them to.
    """

    def test_declared_transfers_get_the_floor_not_the_feed_value(
        self, thursday_timetable: Timetable
    ) -> None:
        """Every TheRide transfer declares 10 s across bays up to 70.5 m apart."""
        declared = [
            footpath for footpath in thursday_timetable.footpaths if footpath.is_declared
        ]
        assert declared

        for footpath in declared:
            assert footpath.declared_seconds == 10
            assert footpath.seconds >= MIN_TRANSFER_SECONDS
            assert footpath.seconds > footpath.declared_seconds

    def test_the_edge_the_feed_omits_is_present_on_its_own_merits(
        self, thursday_timetable: Timetable
    ) -> None:
        """103->101, which used to arrive by transitive closure.

        TheRide declares 103->108 and 108->101 and not 103->101, though the
        bays are about 50 m apart. It is now a generated footpath like any
        other: a fact about where the bays are, not an inference from two hops.
        """
        link = next(
            footpath
            for footpath in thursday_timetable.footpaths
            if (footpath.from_stop[1], footpath.to_stop[1]) == ("103", "101")
        )

        assert not link.is_declared
        assert link.distance_metres < 100
        assert link.seconds >= MIN_TRANSFER_SECONDS

    def test_no_stop_walks_to_itself(self, thursday_timetable: Timetable) -> None:
        """The feed publishes stop->itself rows; waiting is already modelled."""
        assert all(
            footpath.from_stop != footpath.to_stop
            for footpath in thursday_timetable.footpaths
        )

    def test_endpoints_are_agency_qualified(self, thursday_timetable: Timetable) -> None:
        for footpath in thursday_timetable.footpaths:
            assert footpath.from_stop[0] in tuple(AgencySource)
            assert footpath.to_stop[0] in tuple(AgencySource)

    def test_the_two_networks_are_joined(self, thursday_timetable: Timetable) -> None:
        """The M4 acceptance in one assertion: links whose ends differ in agency."""
        crossing = [
            footpath
            for footpath in thursday_timetable.footpaths
            if footpath.from_stop[0] is not footpath.to_stop[0]
        ]

        assert len(crossing) == 1_456

    def test_the_loaded_order_is_stable(self, loaded_engine: Engine) -> None:
        """A shuffled set is a differential mismatch nobody can reproduce."""
        assert load_footpaths(loaded_engine) == load_footpaths(loaded_engine)


@pytest.mark.db
class TestBoardingRestrictions:
    def test_no_pickup_stops_are_marked_unboardable(
        self, thursday_timetable: Timetable
    ) -> None:
        """TheRide marks 988 stop_times no-pickup and 576 no-drop-off."""
        unboardable = sum(
            1
            for instance in thursday_timetable.instances
            if instance.service_date == THURSDAY
            for trip_stop in instance.trip.stops
            if not trip_stop.can_board
        )
        unalightable = sum(
            1
            for instance in thursday_timetable.instances
            if instance.service_date == THURSDAY
            for trip_stop in instance.trip.stops
            if not trip_stop.can_alight
        )

        assert unboardable > 0
        assert unalightable > 0

    def test_stops_are_agency_qualified(self, thursday_timetable: Timetable) -> None:
        """stop_id 161 exists in both feeds as different places."""
        both = thursday_timetable.find_stops_by_id("161")

        assert {stop.agency for stop in both} == {AgencySource.THERIDE, AgencySource.MBUS}
        assert {stop.name for stop in both} == {"Tyler + Zephyr", "TEST STOP 1"}
