"""M4 acceptance: door to door, from a point that is not a stop.

The design claim under test is that a place needs no engine support — it is a
stop no vehicle serves, joined to the network by footpaths — so the strongest
assertion here is the differential one: both engines answer a door-to-door query
and agree, using the code paths they already had.

Coordinates, not addresses. Geocoding is a live third-party service and belongs
behind the `network` marker; the routing does not depend on it.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import Engine

from a2transit.db.models import AgencySource
from a2transit.routing.engine import plan_with_raptor
from a2transit.routing.patterns import RaptorTimetable, build_raptor_timetable
from a2transit.routing.places import (
    ACCESS_MAX_METRES,
    DIRECT_WALK_MAX_METRES,
    Place,
    PseudoAgency,
    attach_places,
    nearby_stops,
    with_places,
    with_places_raptor,
)
from a2transit.routing.search import plan as dijkstra_plan
from a2transit.routing.timetable import Timetable, build_timetable
from tests.conftest import load_real_feeds

pytestmark = pytest.mark.db

THURSDAY = dt.date(2026, 9, 10)
NINE_AM = dt.datetime(2026, 9, 10, 9, 0)
THERIDE = AgencySource.THERIDE
MBUS = AgencySource.MBUS

KERRYTOWN = Place("Kerrytown Market", 42.2846, -83.7454)
MICHIGAN_STADIUM = Place("Michigan Stadium", 42.2658, -83.7478)
#: Two blocks from Kerrytown: close enough that walking beats waiting.
ZINGERMANS = Place("Zingerman's Delicatessen", 42.2851, -83.7440)
#: Farm country west of Dexter — inside the geocoder's box, outside the network.
NOWHERE = Place("A field near Chelsea", 42.36, -84.02)


@pytest.fixture(scope="module")
def engine(db_engine: Engine) -> Engine:
    load_real_feeds(db_engine, patterns=True)
    return db_engine


@pytest.fixture(scope="module")
def raptor_timetable(engine: Engine) -> RaptorTimetable:
    return build_raptor_timetable(engine, THURSDAY)


@pytest.fixture(scope="module")
def dijkstra_timetable(engine: Engine) -> Timetable:
    return build_timetable(engine, THURSDAY)


class TestNearbyStops:
    def test_the_nearest_stops_come_first(self, engine: Engine) -> None:
        found = nearby_stops(engine, KERRYTOWN)

        assert found
        assert [stop.metres for stop in found] == sorted(stop.metres for stop in found)
        assert found[0].metres < 200

    def test_nothing_beyond_the_access_radius(self, engine: Engine) -> None:
        for stop in nearby_stops(engine, KERRYTOWN):
            assert stop.metres <= ACCESS_MAX_METRES

    def test_unserved_stops_are_not_offered(self, engine: Engine) -> None:
        """MBus ships TEST STOP 2, 85 m from Michigan Stadium and never called at.

        It is closer than any real candidate, so without the filter it takes a
        slot and the rider is offered a walk to a stop no bus visits.
        """
        names = {stop.name for stop in nearby_stops(engine, MICHIGAN_STADIUM)}

        assert not any(name.startswith("TEST STOP") for name in names)

    def test_the_result_is_deterministic(self, engine: Engine) -> None:
        """Equidistant stops must not come back in planner order."""
        assert nearby_stops(engine, KERRYTOWN) == nearby_stops(engine, KERRYTOWN)

    def test_somewhere_with_no_service_finds_nothing(self, engine: Engine) -> None:
        assert nearby_stops(engine, NOWHERE) == ()


class TestDoorToDoor:
    """The M4 acceptance criterion."""

    def test_both_engines_plan_the_same_journey(
        self,
        engine: Engine,
        raptor_timetable: RaptorTimetable,
        dijkstra_timetable: Timetable,
    ) -> None:
        attachment = attach_places(engine, KERRYTOWN, MICHIGAN_STADIUM)

        outcome = plan_with_raptor(
            with_places_raptor(raptor_timetable, attachment),
            attachment.origin,
            attachment.destination,
            NINE_AM,
        )
        reference = dijkstra_plan(
            engine,
            attachment.origin,
            attachment.destination,
            NINE_AM,
            timetable=with_places(dijkstra_timetable, attachment),
        ).itinerary

        assert outcome.fastest is not None
        assert reference is not None
        assert outcome.fastest.arrival == reference.arrival

    def test_the_journey_starts_and_ends_on_foot(
        self, engine: Engine, raptor_timetable: RaptorTimetable
    ) -> None:
        attachment = attach_places(engine, KERRYTOWN, MICHIGAN_STADIUM)
        itinerary = plan_with_raptor(
            with_places_raptor(raptor_timetable, attachment),
            attachment.origin,
            attachment.destination,
            NINE_AM,
        ).fastest

        assert itinerary.legs[0].from_stop.key == attachment.origin
        assert itinerary.legs[-1].to_stop.key == attachment.destination
        assert itinerary.ride_legs, "a 2 km trip should not be walked"

    def test_the_legs_chain_from_the_front_door(
        self, engine: Engine, raptor_timetable: RaptorTimetable
    ) -> None:
        attachment = attach_places(engine, KERRYTOWN, MICHIGAN_STADIUM)
        itinerary = plan_with_raptor(
            with_places_raptor(raptor_timetable, attachment),
            attachment.origin,
            attachment.destination,
            NINE_AM,
        ).fastest

        for before, after in zip(itinerary.legs, itinerary.legs[1:], strict=False):
            assert before.to_stop.key == after.from_stop.key
            assert before.arrive <= after.depart

    def test_a_short_hop_is_answered_with_a_walk(
        self, engine: Engine, raptor_timetable: RaptorTimetable
    ) -> None:
        """Two blocks. A planner that suggests a bus here looks ridiculous."""
        attachment = attach_places(engine, KERRYTOWN, ZINGERMANS)
        outcome = plan_with_raptor(
            with_places_raptor(raptor_timetable, attachment),
            attachment.origin,
            attachment.destination,
            NINE_AM,
        )

        assert attachment.direct_walk_metres is not None
        assert attachment.direct_walk_metres < DIRECT_WALK_MAX_METRES
        assert outcome.fastest.ride_legs == ()
        assert len(outcome.fastest.legs) == 1

    def test_an_unreachable_place_is_reported_rather_than_guessed(
        self, engine: Engine
    ) -> None:
        attachment = attach_places(engine, NOWHERE, MICHIGAN_STADIUM)

        assert not attachment.is_routable
        assert attachment.origin_stops == ()


class TestTheSyntheticStopIsWellBehaved:
    def test_a_place_key_is_not_in_either_feed_namespace(self) -> None:
        attachment_key = PseudoAgency.PLACE

        assert attachment_key not in tuple(AgencySource)
        assert attachment_key.value == "place"

    def test_attaching_does_not_mutate_the_shared_timetable(
        self, engine: Engine, raptor_timetable: RaptorTimetable
    ) -> None:
        """One timetable is reused across every query on a date.

        Attaching in place would wire one rider's front door into the next
        rider's network, which is the kind of bug that only shows up under load.
        """
        before_stops = len(raptor_timetable.stops)
        before_footpaths = len(raptor_timetable.footpaths)

        attachment = attach_places(engine, KERRYTOWN, MICHIGAN_STADIUM)
        attached = with_places_raptor(raptor_timetable, attachment)

        assert len(raptor_timetable.stops) == before_stops
        assert len(raptor_timetable.footpaths) == before_footpaths
        assert len(attached.stops) == before_stops + 2

    def test_no_pattern_serves_a_place(
        self, engine: Engine, raptor_timetable: RaptorTimetable
    ) -> None:
        """So the only way out of it is on foot, which is the entire design."""
        attachment = attach_places(engine, KERRYTOWN, MICHIGAN_STADIUM)
        attached = with_places_raptor(raptor_timetable, attachment)

        assert attached.stop_index.get(attachment.origin) is None
        assert attached.footpaths[attachment.origin]
