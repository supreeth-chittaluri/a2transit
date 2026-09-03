"""The independent oracle for the fewest-transfers criterion.

M2's plain Dijkstra answers only "earliest arrival", so checking RAPTOR against
it verifies exactly one entry of the Pareto set. The bounded search extends M2
with a vehicle budget — labels become (node, boardings) over the same
time-expanded DAG — so every entry gets an oracle, derived by a different
algorithm family than RAPTOR uses.

It earned its keep immediately: on its first run it caught RAPTOR boarding a
bus hours past the horizon, because the horizon check bounded when the rider
was *ready* rather than when the vehicle *departed*.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import Engine

from a2transit.db.models import AgencySource
from a2transit.ingest.loader import load_from_path
from a2transit.preprocess.patterns import build_patterns
from a2transit.routing.bounded import bounded_curve
from a2transit.routing.engine import plan_with_raptor
from a2transit.routing.patterns import RaptorTimetable, build_raptor_timetable
from a2transit.routing.timetable import Timetable, build_timetable
from tests.conftest import DATA_DIR

pytestmark = pytest.mark.db

THURSDAY = dt.date(2026, 9, 10)
THERIDE = AgencySource.THERIDE
MBUS = AgencySource.MBUS
MAX_BOARDINGS = 5

#: Pairs chosen to span the interesting shapes: a direct trip, a genuine
#: transfers-vs-time trade-off, and journeys needing three and four vehicles.
ORACLE_PAIRS = [
    ((THERIDE, "1338"), (THERIDE, "1605"), dt.datetime(2026, 9, 10, 6, 0)),
    ((THERIDE, "544"), (THERIDE, "1019"), dt.datetime(2026, 9, 10, 9, 0)),
    ((MBUS, "218"), (MBUS, "247"), dt.datetime(2026, 9, 10, 8, 45)),
    ((MBUS, "207"), (MBUS, "215"), dt.datetime(2026, 9, 10, 10, 0)),
    ((THERIDE, "606"), (THERIDE, "354"), dt.datetime(2026, 9, 10, 8, 45)),
    ((THERIDE, "353"), (THERIDE, "658"), dt.datetime(2026, 9, 10, 10, 45)),
]

IDS = [f"{o[1]}->{d[1]}@{t:%H%M}" for o, d, t in ORACLE_PAIRS]


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
def dijkstra_timetable(engine: Engine) -> Timetable:
    return build_timetable(engine, THURSDAY)


@pytest.fixture(scope="module")
def raptor_timetable(engine: Engine) -> RaptorTimetable:
    return build_raptor_timetable(engine, THURSDAY)


def _raptor_bounded(outcome, budget: int) -> dt.datetime | None:
    """RAPTOR's best arrival using at most `budget` vehicles."""
    candidates = [
        itinerary.arrival
        for itinerary in outcome.itineraries
        if len(itinerary.ride_legs) <= budget
    ]
    return min(candidates) if candidates else None


class TestRaptorMatchesTheOracleAtEveryBudget:
    @pytest.mark.parametrize(("origin", "destination", "departure"), ORACLE_PAIRS, ids=IDS)
    def test_every_vehicle_budget_agrees(
        self,
        engine: Engine,
        dijkstra_timetable: Timetable,
        raptor_timetable: RaptorTimetable,
        origin: tuple[AgencySource, str],
        destination: tuple[AgencySource, str],
        departure: dt.datetime,
    ) -> None:
        curve = bounded_curve(
            engine,
            origin,
            destination,
            departure,
            max_boardings=MAX_BOARDINGS,
            timetable=dijkstra_timetable,
        )
        outcome = plan_with_raptor(raptor_timetable, origin, destination, departure)

        for result in curve:
            expected = _raptor_bounded(outcome, result.max_boardings)
            assert result.arrival == expected, (
                f"budget {result.max_boardings}: oracle {result.arrival}, "
                f"RAPTOR {expected}\n"
                f"reproduce: python -m a2transit.routing --from "
                f"{origin[0].value}:{origin[1]} --to {destination[0].value}:{destination[1]} "
                f"--depart {departure.isoformat()} --compare"
            )


class TestOracleProperties:
    """Sanity checks on the oracle, so a broken oracle cannot pass a broken RAPTOR."""

    @pytest.mark.parametrize(("origin", "destination", "departure"), ORACLE_PAIRS, ids=IDS)
    def test_a_bigger_budget_never_arrives_later(
        self,
        engine: Engine,
        dijkstra_timetable: Timetable,
        origin: tuple[AgencySource, str],
        destination: tuple[AgencySource, str],
        departure: dt.datetime,
    ) -> None:
        curve = bounded_curve(
            engine,
            origin,
            destination,
            departure,
            max_boardings=MAX_BOARDINGS,
            timetable=dijkstra_timetable,
        )

        previous: dt.datetime | None = None
        for result in curve:
            if result.arrival is None:
                assert previous is None, "reachability went backwards as the budget grew"
                continue
            if previous is not None:
                assert result.arrival <= previous
            previous = result.arrival

    def test_zero_budget_reaches_nothing(
        self, engine: Engine, dijkstra_timetable: Timetable
    ) -> None:
        curve = bounded_curve(
            engine,
            (THERIDE, "1338"),
            (THERIDE, "1605"),
            dt.datetime(2026, 9, 10, 6, 0),
            max_boardings=0,
            timetable=dijkstra_timetable,
        )

        assert curve == ()

    @pytest.mark.parametrize(("origin", "destination", "departure"), ORACLE_PAIRS, ids=IDS)
    def test_the_itinerary_respects_its_own_budget(
        self,
        engine: Engine,
        dijkstra_timetable: Timetable,
        origin: tuple[AgencySource, str],
        destination: tuple[AgencySource, str],
        departure: dt.datetime,
    ) -> None:
        curve = bounded_curve(
            engine,
            origin,
            destination,
            departure,
            max_boardings=MAX_BOARDINGS,
            timetable=dijkstra_timetable,
        )

        for result in curve:
            if result.itinerary is None:
                continue
            assert len(result.itinerary.ride_legs) <= result.max_boardings

    def test_the_largest_budget_reproduces_the_unbounded_answer(
        self, engine: Engine, dijkstra_timetable: Timetable
    ) -> None:
        """Where the two oracles overlap, they must agree."""
        from a2transit.routing.search import plan

        for origin, destination, departure in ORACLE_PAIRS:
            unbounded = plan(
                engine, origin, destination, departure, timetable=dijkstra_timetable
            ).itinerary
            curve = bounded_curve(
                engine,
                origin,
                destination,
                departure,
                max_boardings=MAX_BOARDINGS,
                timetable=dijkstra_timetable,
            )
            best = curve[-1].arrival

            assert best == (unbounded.arrival if unbounded else None)


class TestHorizonRegression:
    """Pins the bug the oracle found: a boarding must be inside the horizon.

    RAPTOR bounded the rider's readiness rather than the vehicle's departure, so
    a rider ready at 08:45 could board a bus leaving at 19:00 — for which M2's
    graph has no platform node at all. RAPTOR answered where M2 found nothing.
    """

    def test_no_boarding_past_the_horizon(self, raptor_timetable: RaptorTimetable) -> None:
        departure = dt.datetime(2026, 9, 10, 8, 45)
        horizon = dt.timedelta(hours=6)

        outcome = plan_with_raptor(
            raptor_timetable, (THERIDE, "606"), (THERIDE, "354"), departure
        )

        for itinerary in outcome.itineraries:
            for leg in itinerary.ride_legs:
                assert leg.depart <= departure + horizon

    def test_the_pair_that_exposed_it(
        self,
        engine: Engine,
        dijkstra_timetable: Timetable,
        raptor_timetable: RaptorTimetable,
    ) -> None:
        """Nothing is reachable within three vehicles here; RAPTOR once claimed
        a 19:01:33 arrival."""
        departure = dt.datetime(2026, 9, 10, 8, 45)
        outcome = plan_with_raptor(
            raptor_timetable, (THERIDE, "606"), (THERIDE, "354"), departure
        )

        assert _raptor_bounded(outcome, 3) is None
        assert _raptor_bounded(outcome, 4) == dt.datetime(2026, 9, 10, 10, 51, 48)
