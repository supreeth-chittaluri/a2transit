"""RAPTOR core, checked against the M2 reference on the acceptance pairs.

The exhaustive differential run lives in test_differential.py. These pin the
six journeys M2 already verified against the published schedule, so a RAPTOR
regression shows up against known-good answers rather than only against the
other engine.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import Engine

from a2transit.db.models import AgencySource
from a2transit.ingest.loader import load_from_path
from a2transit.preprocess.patterns import build_patterns
from a2transit.routing.patterns import RaptorTimetable, build_raptor_timetable
from a2transit.routing.raptor import RideStep, TransferStep, pareto_set, run_raptor
from a2transit.routing.search import plan
from a2transit.routing.timetable import Timetable, build_timetable
from tests.conftest import DATA_DIR

pytestmark = pytest.mark.db

THURSDAY = dt.date(2026, 9, 10)
#: The one date in the feed where MBus service 10 runs, and therefore the one
#: date where pattern mbus:WX:0's trips overtake and bisection is unsound.
CHRISTMAS_EVE = dt.date(2026, 12, 24)

THERIDE = AgencySource.THERIDE
MBUS = AgencySource.MBUS


@pytest.fixture(scope="module")
def engine(db_engine: Engine) -> Engine:
    for agency, filename in ((THERIDE, "theride.zip"), (MBUS, "mbus.zip")):
        path = DATA_DIR / filename
        if not path.exists():
            pytest.skip(f"{path} not present; run `python -m a2transit.ingest`")
        load_from_path(db_engine, agency, path)
        build_patterns(db_engine, agency)
    return db_engine


@pytest.fixture(scope="module")
def raptor_thursday(engine: Engine) -> RaptorTimetable:
    return build_raptor_timetable(engine, THURSDAY)


@pytest.fixture(scope="module")
def dijkstra_thursday(engine: Engine) -> Timetable:
    return build_timetable(engine, THURSDAY)


def _seconds(hour: int, minute: int = 0) -> int:
    return hour * 3600 + minute * 60


class TestTimetableParity:
    def test_both_engines_see_the_same_vehicles(
        self, raptor_thursday: RaptorTimetable, dijkstra_thursday: Timetable
    ) -> None:
        """A differential test is meaningless if the inputs differ."""
        assert raptor_thursday.run_count == len(dijkstra_thursday.instances)

    def test_patterns_cover_every_stop_that_has_service(
        self, raptor_thursday: RaptorTimetable
    ) -> None:
        assert len(raptor_thursday.stop_index) > 1000


class TestAcceptancePairs:
    """The six M2 pairs, re-run through RAPTOR."""

    def test_direct_route_4(self, raptor_thursday: RaptorTimetable) -> None:
        result = run_raptor(
            raptor_thursday,
            (THERIDE, "1338"),
            _seconds(6),
            destination=(THERIDE, "1605"),
        )
        entries = pareto_set(result, (THERIDE, "1605"))

        assert len(entries) == 1
        entry = entries[0]
        assert entry.rounds == 1
        assert entry.transfers == 0
        assert raptor_thursday.absolute_time(entry.arrival) == dt.datetime(2026, 9, 10, 6, 43)
        step = entry.steps[0]
        assert isinstance(step, RideStep)
        assert step.run.trip_id == "3572020"

    def test_transfer_via_the_declared_ytc_link(
        self, raptor_thursday: RaptorTimetable
    ) -> None:
        result = run_raptor(
            raptor_thursday,
            (THERIDE, "544"),
            _seconds(9),
            destination=(THERIDE, "1019"),
        )
        entry = pareto_set(result, (THERIDE, "1019"))[-1]

        assert entry.transfers == 1
        assert raptor_thursday.absolute_time(entry.arrival) == dt.datetime(
            2026, 9, 10, 9, 37, 10
        )
        rides = [step for step in entry.steps if isinstance(step, RideStep)]
        walks = [step for step in entry.steps if isinstance(step, TransferStep)]
        assert [ride.run.trip_id for ride in rides] == ["3777020", "3408020"]
        assert len(walks) == 1
        assert (walks[0].from_stop[1], walks[0].to_stop[1]) == ("170", "101")

    def test_post_midnight_arrival(self, raptor_thursday: RaptorTimetable) -> None:
        result = run_raptor(
            raptor_thursday, (MBUS, "275"), _seconds(23, 50), destination=(MBUS, "207")
        )
        entry = pareto_set(result, (MBUS, "207"))[0]

        assert entry.rounds == 1
        assert raptor_thursday.absolute_time(entry.arrival) == dt.datetime(2026, 9, 11, 0, 15)
        assert entry.steps[0].run.trip_id == "2189020"

    def test_three_ride_journey(self, raptor_thursday: RaptorTimetable) -> None:
        result = run_raptor(
            raptor_thursday, (MBUS, "207"), _seconds(10), destination=(MBUS, "215")
        )
        entry = pareto_set(result, (MBUS, "215"))[-1]

        assert entry.transfers == 2
        assert raptor_thursday.absolute_time(entry.arrival) == dt.datetime(2026, 9, 10, 11, 5)
        assert [step.run.trip_id for step in entry.steps if isinstance(step, RideStep)] == [
            "5299020",
            "462020",
            "744020",
        ]

    def test_cross_agency_is_unreachable(self, raptor_thursday: RaptorTimetable) -> None:
        result = run_raptor(
            raptor_thursday, (THERIDE, "1605"), _seconds(9), destination=(MBUS, "207")
        )

        assert pareto_set(result, (MBUS, "207")) == ()

    def test_departure_sensitivity(self, raptor_thursday: RaptorTimetable) -> None:
        early = run_raptor(
            raptor_thursday, (THERIDE, "1338"), _seconds(6), destination=(THERIDE, "1605")
        )
        later = run_raptor(
            raptor_thursday, (THERIDE, "1338"), _seconds(6, 3), destination=(THERIDE, "1605")
        )

        early_entry = pareto_set(early, (THERIDE, "1605"))[0]
        later_entry = pareto_set(later, (THERIDE, "1605"))[0]

        assert early_entry.steps[0].run.trip_id == "3572020"
        assert later_entry.steps[0].run.trip_id == "3212020"
        assert later_entry.arrival > early_entry.arrival


class TestAgreementWithM2:
    """Same six pairs, compared leg by leg against the Dijkstra reference."""

    PAIRS = [
        ((THERIDE, "1338"), (THERIDE, "1605"), _seconds(6)),
        ((THERIDE, "544"), (THERIDE, "1019"), _seconds(9)),
        ((MBUS, "275"), (MBUS, "207"), _seconds(23, 50)),
        ((MBUS, "207"), (MBUS, "215"), _seconds(10)),
        ((THERIDE, "983"), (THERIDE, "205"), _seconds(9)),
        ((THERIDE, "16"), (THERIDE, "161"), _seconds(9, 15)),
    ]

    @pytest.mark.parametrize(
        ("origin", "destination", "at"), PAIRS, ids=[f"{p[0][1]}->{p[1][1]}" for p in PAIRS]
    )
    def test_arrival_and_trips_match(
        self,
        engine: Engine,
        raptor_thursday: RaptorTimetable,
        dijkstra_thursday: Timetable,
        origin: tuple[AgencySource, str],
        destination: tuple[AgencySource, str],
        at: int,
    ) -> None:
        departure = dt.datetime.combine(THURSDAY, dt.time()) + dt.timedelta(seconds=at)
        reference = plan(
            engine, origin, destination, departure, timetable=dijkstra_thursday
        ).itinerary
        assert reference is not None

        result = run_raptor(raptor_thursday, origin, at, destination=destination)
        entries = pareto_set(result, destination)
        assert entries, "RAPTOR found nothing where M2 found a journey"

        # Earliest arrival is the last entry: the Pareto set runs from fewest
        # vehicles to earliest arrival.
        fastest = entries[-1]
        assert raptor_thursday.absolute_time(fastest.arrival) == reference.arrival

        raptor_trips = [
            step.run.trip_id for step in fastest.steps if isinstance(step, RideStep)
        ]
        assert raptor_trips == [leg.trip_id for leg in reference.ride_legs]


class TestOvertakingFallback:
    """The one pattern whose trips overtake, on the one date its service runs."""

    def test_columns_are_sorted_on_an_ordinary_day(
        self, raptor_thursday: RaptorTimetable
    ) -> None:
        unsorted = [p.pattern_id for p in raptor_thursday.patterns if not p.sorted_columns]

        assert unsorted == []

    def test_the_fallback_engages_on_christmas_eve(self, engine: Engine) -> None:
        """MBus service 10 runs on exactly one date in the feed."""
        timetable = build_raptor_timetable(engine, CHRISTMAS_EVE)

        unsorted = [p.pattern_id for p in timetable.patterns if not p.sorted_columns]

        assert unsorted == ["mbus:WX:0"]

    def test_scanning_finds_the_same_earliest_trip_as_bisecting(
        self, engine: Engine
    ) -> None:
        """The fallback must agree with bisection wherever bisection is valid."""
        timetable = build_raptor_timetable(engine, CHRISTMAS_EVE)
        pattern = next(p for p in timetable.patterns if p.pattern_id == "mbus:WX:0")

        for position in range(len(pattern.stops)):
            column = pattern.departure_columns[position]
            for probe in (min(column), max(column), (min(column) + max(column)) // 2):
                found = pattern.earliest_run(position, probe)
                candidates = [time for time in column if time >= probe]
                if not candidates:
                    assert found is None
                else:
                    assert found is not None
                    assert column[found] == min(candidates)

    def test_christmas_eve_still_plans(self, engine: Engine) -> None:
        timetable = build_raptor_timetable(engine, CHRISTMAS_EVE)

        result = run_raptor(
            timetable, (THERIDE, "1338"), _seconds(9), destination=(THERIDE, "1605")
        )

        assert pareto_set(result, (THERIDE, "1605"))
