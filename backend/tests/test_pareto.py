"""The multi-criteria half of RAPTOR: Pareto sets and the extractions from them.

Fewest-transfers has no M2 oracle — M2 answers earliest arrival only — so this
module leans on hand-verified pairs and structural invariants. The bounded
oracle in test_bounded_oracle.py is the third leg.

Hand-verified pair: MBus Domino's Farms Lobby H (218) -> FXB Outbound (247),
Thursday 2026-09-10 departing 08:45. Both options were read out of stop_times
before being written down:

    1 transfer  arrive 09:43:55   trip 2020 (NES) 08:54:33 -> 09:28:00 at stop 222
                                  trip 580020 (MX) 09:30:00 -> 09:43:55
    2 transfers arrive 09:38:59   trip 2020 (NES) 08:54:33 -> 09:20:53 at stop 241
                                  trip 1577020 (CS) 09:22:30 -> 09:22:54 at stop 243
                                  trip 301020 (NES) 09:37:18 -> 09:38:59

One fewer transfer costs just under five minutes, which is exactly the trade-off
the second criterion exists to expose.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import Engine

from a2transit.db.models import AgencySource
from a2transit.ingest.loader import load_from_path
from a2transit.preprocess.patterns import build_patterns
from a2transit.routing.engine import PlanOutcome, plan_with_raptor
from a2transit.routing.patterns import RaptorTimetable, build_raptor_timetable
from tests.conftest import DATA_DIR

pytestmark = pytest.mark.db

THURSDAY = dt.date(2026, 9, 10)
THERIDE = AgencySource.THERIDE
MBUS = AgencySource.MBUS

TRADE_OFF_ORIGIN = (MBUS, "218")
TRADE_OFF_DESTINATION = (MBUS, "247")
TRADE_OFF_DEPARTURE = dt.datetime(2026, 9, 10, 8, 45)


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
def timetable(engine: Engine) -> RaptorTimetable:
    return build_raptor_timetable(engine, THURSDAY)


@pytest.fixture(scope="module")
def trade_off(timetable: RaptorTimetable) -> PlanOutcome:
    return plan_with_raptor(
        timetable, TRADE_OFF_ORIGIN, TRADE_OFF_DESTINATION, TRADE_OFF_DEPARTURE
    )


class TestHandVerifiedTradeOff:
    def test_both_options_are_offered(self, trade_off: PlanOutcome) -> None:
        assert len(trade_off.itineraries) == 2
        assert [it.transfer_count for it in trade_off.itineraries] == [1, 2]

    def test_the_one_transfer_option_matches_the_schedule(
        self, trade_off: PlanOutcome
    ) -> None:
        itinerary = trade_off.itineraries[0]

        assert itinerary.arrival == dt.datetime(2026, 9, 10, 9, 43, 55)
        assert [leg.trip_id for leg in itinerary.ride_legs] == ["2020", "580020"]
        assert [leg.route_label for leg in itinerary.ride_legs] == ["NES", "MX"]
        first, second = itinerary.ride_legs
        assert first.depart == dt.datetime(2026, 9, 10, 8, 54, 33)
        assert first.to_stop.stop_id == "222"
        assert second.depart == dt.datetime(2026, 9, 10, 9, 30)

    def test_the_two_transfer_option_matches_the_schedule(
        self, trade_off: PlanOutcome
    ) -> None:
        itinerary = trade_off.itineraries[1]

        assert itinerary.arrival == dt.datetime(2026, 9, 10, 9, 38, 59)
        assert [leg.trip_id for leg in itinerary.ride_legs] == ["2020", "1577020", "301020"]
        assert [leg.route_label for leg in itinerary.ride_legs] == ["NES", "CS", "NES"]

    def test_one_fewer_transfer_costs_just_under_five_minutes(
        self, trade_off: PlanOutcome
    ) -> None:
        fewer, faster = trade_off.itineraries

        assert fewer.transfer_count < faster.transfer_count
        assert fewer.arrival > faster.arrival
        assert fewer.arrival - faster.arrival == dt.timedelta(minutes=4, seconds=56)

    def test_fastest_and_fewest_transfers_disagree_here(
        self, trade_off: PlanOutcome
    ) -> None:
        """If they agreed, this pair would prove nothing about the criterion."""
        assert trade_off.fastest is not trade_off.fewest_transfers
        assert trade_off.fastest.arrival == dt.datetime(2026, 9, 10, 9, 38, 59)
        assert trade_off.fewest_transfers.transfer_count == 1


class TestArrivingByDeadline:
    """The extraction with the least oracle coverage, pinned explicitly.

    "Of the journeys arriving by T, which uses fewest transfers" is neither the
    fastest option nor the fewest-transfers option in general — it is the
    question a rider with an appointment asks, and the answer changes with T.
    """

    def test_a_generous_deadline_takes_the_simplest_journey(
        self, trade_off: PlanOutcome
    ) -> None:
        chosen = trade_off.arriving_by(dt.datetime(2026, 9, 10, 9, 45))

        assert chosen is not None
        assert chosen.transfer_count == 1
        assert chosen.arrival == dt.datetime(2026, 9, 10, 9, 43, 55)

    def test_a_deadline_between_the_options_forces_the_extra_transfer(
        self, trade_off: PlanOutcome
    ) -> None:
        """09:40 rules out the 09:43:55 option, so the 2-transfer one wins."""
        chosen = trade_off.arriving_by(dt.datetime(2026, 9, 10, 9, 40))

        assert chosen is not None
        assert chosen.transfer_count == 2
        assert chosen.arrival == dt.datetime(2026, 9, 10, 9, 38, 59)

    def test_a_deadline_exactly_on_an_arrival_is_met(self, trade_off: PlanOutcome) -> None:
        chosen = trade_off.arriving_by(dt.datetime(2026, 9, 10, 9, 43, 55))

        assert chosen is not None
        assert chosen.transfer_count == 1

    def test_an_impossible_deadline_returns_nothing(self, trade_off: PlanOutcome) -> None:
        assert trade_off.arriving_by(dt.datetime(2026, 9, 10, 9, 30)) is None

    def test_the_answer_never_uses_more_transfers_as_the_deadline_relaxes(
        self, trade_off: PlanOutcome
    ) -> None:
        """Monotonicity: more time available can never require more transfers."""
        previous: int | None = None
        for minute in range(35, 60, 2):
            deadline = dt.datetime(2026, 9, 10, 9, minute)
            chosen = trade_off.arriving_by(deadline)
            if chosen is None:
                continue
            if previous is not None:
                assert chosen.transfer_count <= previous
            previous = chosen.transfer_count


INVARIANT_PAIRS = [
    ((MBUS, "218"), (MBUS, "247"), dt.datetime(2026, 9, 10, 8, 45)),
    ((THERIDE, "606"), (THERIDE, "354"), dt.datetime(2026, 9, 10, 8, 45)),
    ((THERIDE, "353"), (THERIDE, "658"), dt.datetime(2026, 9, 10, 10, 45)),
    ((THERIDE, "1338"), (THERIDE, "1605"), dt.datetime(2026, 9, 10, 6, 0)),
    ((THERIDE, "544"), (THERIDE, "1019"), dt.datetime(2026, 9, 10, 9, 0)),
    ((MBUS, "207"), (MBUS, "215"), dt.datetime(2026, 9, 10, 10, 0)),
]


@pytest.fixture(scope="module")
def outcomes(timetable: RaptorTimetable) -> list[PlanOutcome]:
    return [
        plan_with_raptor(timetable, origin, destination, departure)
        for origin, destination, departure in INVARIANT_PAIRS
    ]


class TestParetoInvariants:
    """Structural properties every Pareto set must satisfy, on real queries."""

    def test_transfers_increase_and_arrivals_strictly_decrease(
        self, outcomes: list[PlanOutcome]
    ) -> None:
        """The defining property: no entry may be dominated by another."""
        for outcome in outcomes:
            transfers = [it.transfer_count for it in outcome.itineraries]
            arrivals = [it.arrival for it in outcome.itineraries]

            assert transfers == sorted(transfers)
            assert len(set(transfers)) == len(transfers)
            assert arrivals == sorted(arrivals, reverse=True)
            assert len(set(arrivals)) == len(arrivals)

    def test_leg_count_matches_the_declared_transfer_count(
        self, outcomes: list[PlanOutcome]
    ) -> None:
        """Catches a label written into the wrong round."""
        for outcome in outcomes:
            for itinerary in outcome.itineraries:
                assert len(itinerary.ride_legs) == itinerary.transfer_count + 1

    def test_legs_chain_and_time_moves_forward(self, outcomes: list[PlanOutcome]) -> None:
        for outcome in outcomes:
            for itinerary in outcome.itineraries:
                assert itinerary.legs[0].from_stop.key == itinerary.origin.key
                assert itinerary.legs[-1].to_stop.key == itinerary.destination.key
                for earlier, later in zip(
                    itinerary.legs, itinerary.legs[1:], strict=False
                ):
                    assert earlier.to_stop.key == later.from_stop.key
                    assert earlier.arrive == later.depart
                assert itinerary.departure >= itinerary.requested_departure

    def test_every_transfer_clears_the_floor(self, outcomes: list[PlanOutcome]) -> None:
        for outcome in outcomes:
            for itinerary in outcome.itineraries:
                for index, leg in enumerate(itinerary.legs):
                    if index % 2 == 1:  # transfer legs sit between rides
                        assert leg.duration >= dt.timedelta(seconds=60)

    def test_no_boarding_happens_before_the_query_time(
        self, outcomes: list[PlanOutcome]
    ) -> None:
        for outcome in outcomes:
            for itinerary in outcome.itineraries:
                for leg in itinerary.ride_legs:
                    assert leg.depart >= itinerary.requested_departure

    def test_round_0_yields_nothing(self, timetable: RaptorTimetable) -> None:
        """Walking-only journeys do not exist before M4 gives us footpaths."""
        outcome = plan_with_raptor(
            timetable, (THERIDE, "170"), (THERIDE, "101"), dt.datetime(2026, 9, 10, 9, 0)
        )

        assert all(it.transfer_count >= 0 for it in outcome.itineraries)
        assert all(len(it.ride_legs) >= 1 for it in outcome.itineraries)
