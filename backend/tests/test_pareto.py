"""The multi-criteria half of RAPTOR: Pareto sets and the extractions from them.

Fewest-transfers has no M2 oracle — M2 answers earliest arrival only — so this
module leans on hand-verified pairs and structural invariants. The bounded
oracle in test_bounded_oracle.py is the third leg.

Hand-verified pair: TheRide Huron Pkwy + HHS (357) -> Jackson + Grandview
(1330), Thursday 2026-09-10 departing 08:45. Three non-dominated options:

    1 transfer   arrive 10:05:55   theride 3,  then theride 30
    2 transfers  arrive 09:51:__   theride 66, mbus CS, theride 31, then a walk
    3 transfers  arrive 09:35:55   theride 66, mbus CS, theride 61, theride 30

Half an hour bought with two extra changes, and the middle option ends on foot
— which is what makes this a better pair than M3's. It also crosses between the
agencies twice, so it only exists at all because of M4's footpaths.

The earlier pair (MBus 218 -> 247 at the same time) was retired here: with
footpaths there is now a zero-transfer journey arriving 09:19:25 that dominates
both of its options, so it no longer demonstrates a trade-off. It survives in
INVARIANT_PAIRS.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import Engine

from a2transit.db.models import AgencySource
from a2transit.routing.engine import PlanOutcome, plan_with_raptor
from a2transit.routing.patterns import RaptorTimetable, build_raptor_timetable
from tests.conftest import load_real_feeds

pytestmark = pytest.mark.db

THURSDAY = dt.date(2026, 9, 10)
THERIDE = AgencySource.THERIDE
MBUS = AgencySource.MBUS

TRADE_OFF_ORIGIN = (THERIDE, "357")
TRADE_OFF_DESTINATION = (THERIDE, "1330")
TRADE_OFF_DEPARTURE = dt.datetime(2026, 9, 10, 8, 45)


@pytest.fixture(scope="module")
def engine(db_engine: Engine) -> Engine:
    load_real_feeds(db_engine, patterns=True)
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
    def test_all_three_options_are_offered(self, trade_off: PlanOutcome) -> None:
        assert [it.transfer_count for it in trade_off.itineraries] == [1, 2, 3]

    def test_the_one_transfer_option_matches_the_schedule(
        self, trade_off: PlanOutcome
    ) -> None:
        itinerary = trade_off.itineraries[0]

        assert itinerary.arrival == dt.datetime(2026, 9, 10, 10, 5, 55)
        assert [leg.route_label for leg in itinerary.ride_legs] == ["3", "30"]
        first, second = itinerary.ride_legs
        assert first.depart == dt.datetime(2026, 9, 10, 9, 14)
        assert second.depart == dt.datetime(2026, 9, 10, 10, 0)

    def test_the_fastest_option_matches_the_schedule(
        self, trade_off: PlanOutcome
    ) -> None:
        itinerary = trade_off.itineraries[-1]

        assert itinerary.arrival == dt.datetime(2026, 9, 10, 9, 35, 55)
        assert [leg.route_label for leg in itinerary.ride_legs] == ["66", "CS", "61", "30"]

    def test_the_journey_crosses_between_the_agencies(
        self, trade_off: PlanOutcome
    ) -> None:
        """Which is why this pair does not exist before M4 at all."""
        agencies = {leg.agency for leg in trade_off.itineraries[-1].ride_legs}

        assert agencies == {THERIDE, MBUS}

    def test_two_extra_transfers_buy_half_an_hour(self, trade_off: PlanOutcome) -> None:
        fewest, fastest = trade_off.fewest_transfers, trade_off.fastest

        assert fewest.transfer_count == 1
        assert fastest.transfer_count == 3
        assert fewest.arrival - fastest.arrival == dt.timedelta(minutes=30)

    def test_fastest_and_fewest_transfers_disagree_here(
        self, trade_off: PlanOutcome
    ) -> None:
        """If they agreed, this pair would prove nothing about the criterion."""
        assert trade_off.fastest is not trade_off.fewest_transfers
        assert trade_off.fastest.arrival == dt.datetime(2026, 9, 10, 9, 35, 55)
        assert trade_off.fewest_transfers.transfer_count == 1

    def test_the_middle_option_ends_on_foot(self, trade_off: PlanOutcome) -> None:
        """Walking into the destination is arriving there — M4 fixed that."""
        middle = trade_off.itineraries[1]

        assert middle.legs[-1].to_stop.key == (THERIDE, "1330")
        assert middle.ride_legs[-1].to_stop.key != (THERIDE, "1330")


class TestArrivingByDeadline:
    """The extraction with the least oracle coverage, pinned explicitly.

    "Of the journeys arriving by T, which uses fewest transfers" is neither the
    fastest option nor the fewest-transfers option in general — it is the
    question a rider with an appointment asks, and the answer changes with T.
    """

    def test_a_generous_deadline_takes_the_simplest_journey(
        self, trade_off: PlanOutcome
    ) -> None:
        chosen = trade_off.arriving_by(dt.datetime(2026, 9, 10, 10, 30))

        assert chosen is not None
        assert chosen.transfer_count == 1
        assert chosen.arrival == dt.datetime(2026, 9, 10, 10, 5, 55)

    def test_a_deadline_between_the_options_forces_the_extra_transfer(
        self, trade_off: PlanOutcome
    ) -> None:
        """10:00 rules out the 10:05:55 option, so the 2-transfer one wins."""
        chosen = trade_off.arriving_by(dt.datetime(2026, 9, 10, 10, 0))

        assert chosen is not None
        assert chosen.transfer_count == 2

    def test_a_deadline_exactly_on_an_arrival_is_met(self, trade_off: PlanOutcome) -> None:
        chosen = trade_off.arriving_by(dt.datetime(2026, 9, 10, 10, 5, 55))

        assert chosen is not None
        assert chosen.transfer_count == 1

    def test_an_impossible_deadline_returns_nothing(self, trade_off: PlanOutcome) -> None:
        assert trade_off.arriving_by(dt.datetime(2026, 9, 10, 9, 30)) is None

    def test_the_answer_never_uses_more_transfers_as_the_deadline_relaxes(
        self, trade_off: PlanOutcome
    ) -> None:
        """Monotonicity: more time available can never require more transfers."""
        previous: int | None = None
        for minute in range(35, 59, 2):
            deadline = dt.datetime(2026, 9, 10, 9 if minute < 60 else 10, minute % 60)
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

    def test_round_0_answers_a_walk(self, timetable: RaptorTimetable) -> None:
        """Two Ypsilanti Transit Center bays 29 m apart: just walk.

        Round 0 used to be unreachable — walking into a stop was not arriving
        there — so this query returned a bus ride around the block, or nothing.
        A walk is now the whole journey, and it dominates every bus.
        """
        outcome = plan_with_raptor(
            timetable, (THERIDE, "170"), (THERIDE, "101"), dt.datetime(2026, 9, 10, 9, 0)
        )

        assert len(outcome.itineraries) == 1
        walk = outcome.itineraries[0]
        assert walk.ride_legs == ()
        assert walk.transfer_count == 0
        assert walk.arrival == dt.datetime(2026, 9, 10, 9, 1)
        assert walk.legs[0].from_stop.stop_id == "170"
        assert walk.legs[0].to_stop.stop_id == "101"
