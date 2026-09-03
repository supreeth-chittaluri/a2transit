"""The HTTP surface: /plan, /stops/search, /stops/{...}/departures.

Thin on purpose. The routing is tested against two oracles elsewhere; what is
worth asserting here is the contract — that a stop reference must be
agency-qualified, that both the Pareto set and the route geometry survive the
trip through Pydantic, and that a place and a stop can be mixed in one query.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from a2transit.api.state import timetable_cache
from a2transit.main import create_app
from tests.conftest import load_real_feeds

pytestmark = pytest.mark.db

THURSDAY_9AM = "2026-09-10T09:00"
#: Kerrytown and Michigan Stadium, about 2 km apart.
KERRYTOWN = "42.2846,-83.7454"
STADIUM = "42.2658,-83.7478"


@pytest.fixture(scope="module")
def api(db_engine: Engine, module_monkeypatch) -> TestClient:
    """A client whose routers read the test database, not the developer's.

    The routers reach for `get_engine()` directly rather than taking a
    dependency, which is the right call for a process that has exactly one
    database — but it means the test has to point that one engine at the test
    database, and drop the timetable cache built against the other one.
    """
    load_real_feeds(db_engine, patterns=True)
    for module in ("a2transit.api.plan", "a2transit.api.stops", "a2transit.api.state"):
        module_monkeypatch.setattr(f"{module}.get_engine", lambda: db_engine, raising=False)
    timetable_cache.cache_clear()
    return TestClient(create_app())


class TestPlan:
    def test_a_stop_to_stop_journey(self, api: TestClient) -> None:
        response = api.get(
            "/plan",
            params={"from": "theride:1605", "to": "mbus:207", "depart": THURSDAY_9AM},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["itineraries"], "the cross-agency journey M4 exists for"
        assert body["engine"] == "raptor"
        assert "AAATA/TheRide" in body["attribution"]

    def test_the_whole_pareto_set_is_returned(self, api: TestClient) -> None:
        """Fewest changes first, strictly earlier arrivals after."""
        body = api.get(
            "/plan",
            params={"from": "theride:357", "to": "theride:1330", "depart": "2026-09-10T08:45"},
        ).json()

        transfers = [it["transfers"] for it in body["itineraries"]]
        arrivals = [it["arrival"] for it in body["itineraries"]]

        assert len(transfers) > 1
        assert transfers == sorted(transfers)
        assert arrivals == sorted(arrivals, reverse=True)

    def test_ride_legs_carry_the_published_shape(self, api: TestClient) -> None:
        """Straight lines between stops look wrong on a map; routes wind."""
        body = api.get(
            "/plan",
            params={"from": "theride:1338", "to": "theride:1605", "depart": "2026-09-10T06:00"},
        ).json()

        ride = next(
            leg for leg in body["itineraries"][0]["legs"] if leg["kind"] == "ride"
        )
        assert ride["geometry"]
        assert len(ride["geometry"]) > 2
        for lon, lat in ride["geometry"]:
            assert -84.5 < lon < -83.0
            assert 42.0 < lat < 42.6

    def test_geometry_can_be_turned_off(self, api: TestClient) -> None:
        body = api.get(
            "/plan",
            params={
                "from": "theride:1338",
                "to": "theride:1605",
                "depart": "2026-09-10T06:00",
                "geometry": "false",
            },
        ).json()

        assert all(leg.get("geometry") is None for leg in body["itineraries"][0]["legs"])

    def test_a_place_to_place_journey(self, api: TestClient) -> None:
        body = api.get(
            "/plan", params={"from": KERRYTOWN, "to": STADIUM, "depart": THURSDAY_9AM}
        ).json()

        assert body["itineraries"]
        first = body["itineraries"][0]
        assert first["legs"][0]["kind"] == "walk"
        assert first["legs"][0]["fromStop"]["id"] is None
        assert first["legs"][-1]["toStop"]["id"] is None

    def test_a_place_and_a_stop_mix_in_one_query(self, api: TestClient) -> None:
        response = api.get(
            "/plan", params={"from": KERRYTOWN, "to": "theride:1605", "depart": THURSDAY_9AM}
        )

        assert response.status_code == 200
        assert response.json()["itineraries"]

    def test_a_bare_stop_id_is_refused(self, api: TestClient) -> None:
        """90 stop_ids exist in both feeds as different places."""
        response = api.get(
            "/plan", params={"from": "1605", "to": "mbus:207", "depart": THURSDAY_9AM}
        )

        assert response.status_code == 422
        assert "agency:stop_id" in response.json()["detail"]

    def test_an_unknown_agency_is_refused(self, api: TestClient) -> None:
        response = api.get(
            "/plan", params={"from": "amtrak:1", "to": "mbus:207", "depart": THURSDAY_9AM}
        )

        assert response.status_code == 422

    def test_an_unknown_stop_is_a_404(self, api: TestClient) -> None:
        response = api.get(
            "/plan", params={"from": "theride:999999", "to": "mbus:207", "depart": THURSDAY_9AM}
        )

        assert response.status_code == 404

    def test_somewhere_with_no_service_nearby_is_a_404(self, api: TestClient) -> None:
        response = api.get(
            "/plan", params={"from": "42.36,-84.02", "to": STADIUM, "depart": THURSDAY_9AM}
        )

        assert response.status_code == 404
        assert "no stop within" in response.json()["detail"]

    def test_both_engines_are_reachable_and_agree(self, api: TestClient) -> None:
        params = {"from": "theride:544", "to": "theride:1019", "depart": THURSDAY_9AM}
        raptor = api.get("/plan", params={**params, "engine": "raptor"}).json()
        dijkstra = api.get("/plan", params={**params, "engine": "dijkstra"}).json()

        assert raptor["itineraries"][-1]["arrival"] == dijkstra["itineraries"][0]["arrival"]


class TestStopSearch:
    def test_finds_a_stop_by_substring(self, api: TestClient) -> None:
        body = api.get("/stops/search", params={"q": "ytc"}).json()

        assert body["results"]
        assert all("YTC" in result["name"].upper() for result in body["results"][:3])

    def test_survives_a_typo(self, api: TestClient) -> None:
        """Which is the entire reason for the trigram index."""
        body = api.get("/stops/search", params={"q": "kerytown"}).json()

        assert any("Kerrytown" in result["name"] for result in body["results"])

    def test_results_are_agency_qualified(self, api: TestClient) -> None:
        body = api.get("/stops/search", params={"q": "central"}).json()

        for result in body["results"]:
            assert result["id"].startswith(("theride:", "mbus:"))

    def test_a_one_character_query_is_refused(self, api: TestClient) -> None:
        assert api.get("/stops/search", params={"q": "y"}).status_code == 422


class TestDepartures:
    def test_lists_the_next_departures_in_order(self, api: TestClient) -> None:
        body = api.get(
            "/stops/theride/544/departures", params={"at": THURSDAY_9AM, "limit": 5}
        ).json()

        departures = body["departures"]
        assert departures
        assert [d["departure"] for d in departures] == sorted(
            d["departure"] for d in departures
        )
        assert all(d["inSeconds"] >= 0 for d in departures)

    def test_the_board_follows_the_service_calendar(self, api: TestClient) -> None:
        """Labor Day is the case a raw stop_times query gets wrong tenfold.

        MBus drops from twelve routes to five, by calendar_dates exception. The
        board is read off the same timetable the router uses, so it drops too —
        a board built from stop_times alone would advertise buses that are not
        running.
        """
        def routes(at: str) -> set[str]:
            body = api.get(
                "/stops/mbus/207/departures", params={"at": at, "limit": 100}
            ).json()
            return {d["routeId"] for d in body["departures"]}

        weekday = routes("2026-09-10T12:00")
        holiday = routes("2026-09-07T12:00")

        assert holiday < weekday

    def test_an_unknown_stop_is_a_404(self, api: TestClient) -> None:
        assert api.get("/stops/theride/999999/departures").status_code == 404
