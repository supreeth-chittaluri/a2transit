"""M2 acceptance: itineraries over the real feeds, checked against the schedule.

Every expected value here was read out of `stop_times` directly before being
written down, so these assert the engine against the published schedule rather
than against its own output. The verification queries are in the docstrings so
the next person can repeat them.

Feeds: TheRide and MBus as published 2026-08-23. Thursday 2026-09-10 is an
ordinary weekday (TheRide service 3, MBus service 4); Monday 2026-09-07 is
Labor Day (MBus drops to service 3 — 366 trips over 5 routes instead of 1,668
over 12).
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import Engine, text

from a2transit.db.models import AgencySource
from a2transit.ingest.loader import load_from_path
from a2transit.routing.models import RideLeg, TransferLeg
from a2transit.routing.search import plan
from a2transit.routing.timetable import Timetable, build_timetable
from tests.conftest import DATA_DIR

pytestmark = pytest.mark.db

THURSDAY = dt.date(2026, 9, 10)
LABOR_DAY = dt.date(2026, 9, 7)

THERIDE = AgencySource.THERIDE
MBUS = AgencySource.MBUS


@pytest.fixture(scope="module")
def engine(db_engine: Engine) -> Engine:
    for agency, filename in ((THERIDE, "theride.zip"), (MBUS, "mbus.zip")):
        path = DATA_DIR / filename
        if not path.exists():
            pytest.skip(f"{path} not present; run `python -m a2transit.ingest`")
        load_from_path(db_engine, agency, path)
    return db_engine


@pytest.fixture(scope="module")
def thursday(engine: Engine) -> Timetable:
    return build_timetable(engine, THURSDAY)


@pytest.fixture(scope="module")
def labor_day(engine: Engine) -> Timetable:
    return build_timetable(engine, LABOR_DAY)


def _at(day: dt.date, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hour, minute))


class TestPair1DirectTrip:
    """TheRide route 4: YTC Stop 2 (1338) -> Temp BTC endpt (1605).

        SELECT stop_sequence, stop_id, departure_time FROM stop_times
         WHERE agency_source='theride' AND trip_id='3572020' ORDER BY stop_sequence;

    Trip 3572020 is route 4's first weekday run: 34 stops, 06:02:00 at stop
    1338 through 06:43:00 at stop 1605. Already pinned by the M1 spot-check.
    """

    def test_returns_the_first_route_4_run(self, engine: Engine, thursday: Timetable) -> None:
        result = plan(
            engine, (THERIDE, "1338"), (THERIDE, "1605"), _at(THURSDAY, 6), timetable=thursday
        )

        itinerary = result.itinerary
        assert itinerary is not None
        assert len(itinerary.legs) == 1
        leg = itinerary.legs[0]
        assert isinstance(leg, RideLeg)
        assert leg.trip_id == "3572020"
        assert leg.route_label == "4"
        assert leg.agency is THERIDE
        assert leg.depart == dt.datetime(2026, 9, 10, 6, 2)
        assert leg.arrive == dt.datetime(2026, 9, 10, 6, 43)
        assert leg.intermediate_stops == 32  # 34 stops, minus both endpoints
        assert itinerary.transfer_count == 0


class TestPair2TransferWithSlack:
    """Route 4 -> route 47 across the Ypsilanti Transit Center, 20 minutes' slack.

        -- outbound leg
        SELECT stop_sequence, stop_id, departure_time FROM stop_times
         WHERE agency_source='theride' AND trip_id='3777020'
           AND stop_id IN ('544','170');            -- 09:00:15 -> 09:10:00
        -- the declared transfer
        SELECT min_transfer_time, ST_Distance(a.geog,b.geog) FROM transfers t ...
         WHERE from_stop_id='170' AND to_stop_id='101';   -- 10 s declared, 29.2 m
        -- inbound leg
        SELECT stop_sequence, stop_id, departure_time FROM stop_times
         WHERE agency_source='theride' AND trip_id='3408020'
           AND stop_id IN ('101','1019');           -- 09:30:00 -> 09:37:10

    Chosen deliberately with generous slack rather than a tight pulse: TheRide
    holds connecting buses at its pulse points, but GTFS gives no way to say so
    (that needs transfer_type=1, which neither feed publishes), so the 60 s
    floor rejects tight timed connections. See the known-limitation test below.
    """

    def test_uses_the_declared_ytc_transfer(self, engine: Engine, thursday: Timetable) -> None:
        result = plan(
            engine, (THERIDE, "544"), (THERIDE, "1019"), _at(THURSDAY, 9), timetable=thursday
        )

        itinerary = result.itinerary
        assert itinerary is not None
        assert [type(leg) for leg in itinerary.legs] == [RideLeg, TransferLeg, RideLeg]
        assert itinerary.transfer_count == 1

        first, transfer, second = itinerary.legs
        assert (first.trip_id, first.route_label) == ("3777020", "4")
        assert first.depart == dt.datetime(2026, 9, 10, 9, 0, 15)
        assert first.arrive == dt.datetime(2026, 9, 10, 9, 10)

        assert not transfer.is_same_stop
        assert (transfer.from_stop.stop_id, transfer.to_stop.stop_id) == ("170", "101")

        assert (second.trip_id, second.route_label) == ("3408020", "47")
        assert second.depart == dt.datetime(2026, 9, 10, 9, 30)
        assert second.arrive == dt.datetime(2026, 9, 10, 9, 37, 10)

    def test_the_connection_has_real_slack(self, engine: Engine, thursday: Timetable) -> None:
        """20 minutes — not a pulse the transfer floor could plausibly break."""
        result = plan(
            engine, (THERIDE, "544"), (THERIDE, "1019"), _at(THURSDAY, 9), timetable=thursday
        )

        rides = result.itinerary.ride_legs
        slack = rides[1].depart - rides[0].arrive

        assert slack == dt.timedelta(minutes=20)


class TestPair3PostMidnight:
    """MBus route NW: Northwood V (275) -> Central Campus TC (207), across midnight.

        SELECT stop_sequence, stop_id, departure_time FROM stop_times
         WHERE agency_source='mbus' AND trip_id='2189020' ORDER BY stop_sequence;

    Departs 23:55:00 and arrives 24:15:00 — GTFS time past midnight, which is
    00:15 on Friday. The whole reason stop_times holds integer seconds.
    """

    def test_arrival_falls_on_the_following_calendar_day(
        self, engine: Engine, thursday: Timetable
    ) -> None:
        result = plan(
            engine, (MBUS, "275"), (MBUS, "207"), _at(THURSDAY, 23, 50), timetable=thursday
        )

        itinerary = result.itinerary
        assert itinerary is not None
        assert itinerary.ride_legs[0].trip_id == "2189020"
        assert itinerary.departure == dt.datetime(2026, 9, 10, 23, 55)
        assert itinerary.arrival == dt.datetime(2026, 9, 11, 0, 15)
        assert itinerary.arrival.date() != itinerary.departure.date()
        assert itinerary.duration == dt.timedelta(minutes=25)

    def test_a_query_after_midnight_still_finds_service(
        self, engine: Engine
    ) -> None:
        """00:05 on Friday: the useful buses belong to Thursday's service date."""
        friday_timetable = build_timetable(engine, dt.date(2026, 9, 11))

        result = plan(
            engine,
            (MBUS, "246"),
            (MBUS, "207"),
            dt.datetime(2026, 9, 11, 0, 5),
            timetable=friday_timetable,
        )

        assert result.itinerary is not None
        assert result.itinerary.arrival <= dt.datetime(2026, 9, 11, 1, 0)


class TestPair4HolidayService:
    """MBus Central Campus TC (207) -> Domino's Farms Lobby A (215).

    Route NES serves stop 215 and runs under service 4 (ordinary weekday) but
    not service 3 (the Labor Day reduction, 5 routes instead of 12):

        SELECT DISTINCT route_id FROM trips
         WHERE agency_source='mbus' AND service_id='3';   -- BB,CN,CS,NW,OS
    """

    def test_reachable_on_an_ordinary_weekday(self, engine: Engine, thursday: Timetable) -> None:
        result = plan(engine, (MBUS, "207"), (MBUS, "215"), _at(THURSDAY, 10), timetable=thursday)

        itinerary = result.itinerary
        assert itinerary is not None
        assert itinerary.arrival == dt.datetime(2026, 9, 10, 11, 5)
        assert itinerary.transfer_count == 2
        assert [leg.route_label for leg in itinerary.ride_legs] == ["NW", "NES", "NES"]

    def test_unreachable_on_labor_day(self, engine: Engine, labor_day: Timetable) -> None:
        """The calendar_dates exceptions are what make this come back empty."""
        result = plan(engine, (MBUS, "207"), (MBUS, "215"), _at(LABOR_DAY, 10), timetable=labor_day)

        assert result.itinerary is None

    def test_labor_day_still_runs_the_reduced_network(
        self, engine: Engine, labor_day: Timetable
    ) -> None:
        """Not everything stops — a route that does run on service 3 still plans."""
        result = plan(engine, (MBUS, "275"), (MBUS, "207"), _at(LABOR_DAY, 12), timetable=labor_day)

        itinerary = result.itinerary
        assert itinerary is not None
        assert itinerary.ride_legs[0].route_label == "NW"
        # A different trip from Thursday's: service 3, not service 4.
        assert itinerary.ride_legs[0].trip_id != "2189020"


class TestPair5Unreachable:
    def test_cross_agency_is_unreachable_until_m4(
        self, engine: Engine, thursday: Timetable
    ) -> None:
        """Blake Transit Center to Central Campus — 2 km apart, no path in M2.

        The feeds share no stop_id, and the only inter-stop links are TheRide's
        own YTC transfers, so nothing joins the two networks. PostGIS footpaths
        in M4 are what make this answerable; until then an empty result is
        correct rather than a bug.
        """
        result = plan(
            engine, (THERIDE, "1605"), (MBUS, "207"), _at(THURSDAY, 9), timetable=thursday
        )

        assert result.itinerary is None

    def test_an_overnight_query_waits_for_the_first_morning_bus(
        self, engine: Engine, thursday: Timetable
    ) -> None:
        """03:00 is outside service hours, but waiting is a valid itinerary."""
        result = plan(
            engine, (THERIDE, "1338"), (THERIDE, "1605"), _at(THURSDAY, 3), timetable=thursday
        )

        itinerary = result.itinerary
        assert itinerary is not None
        assert itinerary.ride_legs[0].trip_id == "3572020"
        assert itinerary.departure == dt.datetime(2026, 9, 10, 6, 2)
        assert itinerary.initial_wait == dt.timedelta(hours=3, minutes=2)

    def test_nothing_is_reachable_within_a_horizon_that_has_no_service(
        self, engine: Engine, thursday: Timetable
    ) -> None:
        """Same 03:00 query, but only looking one hour ahead."""
        result = plan(
            engine,
            (THERIDE, "1338"),
            (THERIDE, "1605"),
            _at(THURSDAY, 3),
            horizon_seconds=3600,
            timetable=thursday,
        )

        assert result.itinerary is None


class TestPair6DepartureSensitivity:
    """Leaving three minutes later means a different bus and a later arrival.

    Route 4's first two weekday runs from stop 1338 leave at 06:02 (trip
    3572020) and 06:10 (trip 3212020).
    """

    def test_three_minutes_later_catches_the_next_run(
        self, engine: Engine, thursday: Timetable
    ) -> None:
        early = plan(
            engine, (THERIDE, "1338"), (THERIDE, "1605"), _at(THURSDAY, 6), timetable=thursday
        ).itinerary
        later = plan(
            engine, (THERIDE, "1338"), (THERIDE, "1605"), _at(THURSDAY, 6, 3), timetable=thursday
        ).itinerary

        assert early.ride_legs[0].trip_id == "3572020"
        assert later.ride_legs[0].trip_id == "3212020"
        assert later.departure == dt.datetime(2026, 9, 10, 6, 10)
        assert later.arrival == dt.datetime(2026, 9, 10, 6, 51)
        assert later.arrival > early.arrival

    def test_arrival_never_improves_by_leaving_later(
        self, engine: Engine, thursday: Timetable
    ) -> None:
        """Monotonicity: a later query cannot produce an earlier arrival."""
        arrivals = []
        for minute in range(0, 60, 10):
            result = plan(
                engine,
                (THERIDE, "1338"),
                (THERIDE, "1605"),
                _at(THURSDAY, 8, minute),
                timetable=thursday,
            )
            assert result.itinerary is not None
            arrivals.append(result.itinerary.arrival)

        assert arrivals == sorted(arrivals)


class TestKnownLimitations:
    def test_tight_timed_transfers_at_pulse_points_are_rejected(
        self, engine: Engine, thursday: Timetable
    ) -> None:
        """Documented M2 limitation, asserted so it cannot regress silently.

        TheRide's declared transfers all claim min_transfer_time = 10 s across
        bays up to 71 m apart — 25 km/h on foot. The 60 s floor overrides that,
        which means a genuine held connection tighter than 60 s is rejected.
        GTFS offers no way for the agency to mark a guaranteed timed transfer
        that both feeds actually use, so there is nothing in the data to tell a
        held connection from a coincidental one.
        """
        with engine.connect() as connection:
            declared = connection.execute(
                text(
                    "SELECT DISTINCT min_transfer_time FROM transfers "
                    "WHERE agency_source = 'theride'"
                )
            ).scalars().all()

        assert declared == [10]
        for transfer in thursday.transfers:
            assert transfer.seconds == 60
            assert transfer.declared_seconds == 10


@pytest.fixture(scope="module")
def itineraries(engine: Engine, thursday: Timetable) -> list:
    pairs = [
        ((THERIDE, "1338"), (THERIDE, "1605"), _at(THURSDAY, 6)),
        ((THERIDE, "544"), (THERIDE, "1019"), _at(THURSDAY, 9)),
        ((MBUS, "275"), (MBUS, "207"), _at(THURSDAY, 23, 50)),
        ((MBUS, "207"), (MBUS, "215"), _at(THURSDAY, 10)),
        ((THERIDE, "983"), (THERIDE, "205"), _at(THURSDAY, 9)),
        ((THERIDE, "16"), (THERIDE, "161"), _at(THURSDAY, 9, 15)),
    ]
    found = []
    for origin, destination, departure in pairs:
        result = plan(engine, origin, destination, departure, timetable=thursday)
        if result.itinerary is not None:
            found.append(result.itinerary)
    assert len(found) == len(pairs), "a verified pair stopped planning"
    return found


class TestItineraryConsistency:
    """Structural properties every itinerary must satisfy, on real data."""

    def test_legs_chain_from_origin_to_destination(self, itineraries: list) -> None:
        for itinerary in itineraries:
            assert itinerary.legs[0].from_stop.key == itinerary.origin.key
            assert itinerary.legs[-1].to_stop.key == itinerary.destination.key
            for earlier, later in zip(itinerary.legs, itinerary.legs[1:], strict=False):
                assert earlier.to_stop.key == later.from_stop.key, itinerary.describe()

    def test_time_never_runs_backwards(self, itineraries: list) -> None:
        for itinerary in itineraries:
            assert itinerary.departure >= itinerary.requested_departure
            for earlier, later in zip(itinerary.legs, itinerary.legs[1:], strict=False):
                assert earlier.arrive == later.depart, itinerary.describe()
            for leg in itinerary.legs:
                assert leg.arrive >= leg.depart

    def test_every_transfer_clears_the_floor(self, itineraries: list) -> None:
        for itinerary in itineraries:
            for leg in itinerary.legs:
                if isinstance(leg, TransferLeg):
                    assert leg.duration >= dt.timedelta(seconds=60), itinerary.describe()

    def test_every_ride_leg_matches_a_real_trip_in_the_right_order(
        self, engine: Engine, itineraries: list
    ) -> None:
        """The strongest check: re-derive each leg straight from stop_times."""
        with engine.connect() as connection:
            for itinerary in itineraries:
                for leg in itinerary.ride_legs:
                    row = connection.execute(
                        text(
                            """
                            SELECT
                              (SELECT stop_sequence FROM stop_times
                                WHERE agency_source = :agency AND trip_id = :trip
                                  AND stop_id = :from_stop) AS from_seq,
                              (SELECT stop_sequence FROM stop_times
                                WHERE agency_source = :agency AND trip_id = :trip
                                  AND stop_id = :to_stop) AS to_seq,
                              (SELECT route_id FROM trips
                                WHERE agency_source = :agency AND trip_id = :trip) AS route_id
                            """
                        ),
                        {
                            "agency": leg.agency.value,
                            "trip": leg.trip_id,
                            "from_stop": leg.from_stop.stop_id,
                            "to_stop": leg.to_stop.stop_id,
                        },
                    ).one()

                    assert row.from_seq is not None, f"{leg.trip_id} never serves its origin"
                    assert row.to_seq is not None, f"{leg.trip_id} never serves its destination"
                    assert row.from_seq < row.to_seq, "leg rides the trip backwards"
                    assert row.route_id == leg.route_id
