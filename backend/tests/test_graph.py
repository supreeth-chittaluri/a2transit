"""Time-expanded graph construction.

The property that matters most is that every edge points forward in time. That
is what makes the graph a DAG, which is what makes earliest-arrival search
correct without any argument about FIFO or non-overtaking. It is asserted here
against both a hand-built timetable and the real feeds.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import Engine

from a2transit.db.models import AgencySource
from a2transit.ingest.loader import load_from_path
from a2transit.routing.constants import MIN_TRANSFER_SECONDS
from a2transit.routing.graph import EdgeKind, NodeKind, build_graph
from a2transit.routing.timetable import (
    Route,
    Stop,
    Timetable,
    TransferLink,
    Trip,
    TripInstance,
    TripStop,
)
from tests.conftest import DATA_DIR

THURSDAY = dt.date(2026, 9, 10)
A = AgencySource.THERIDE


def _stop(stop_id: str, lat: float = 42.28, lon: float = -83.74) -> Stop:
    return Stop((A, stop_id), stop_id, A, f"Stop {stop_id}", lat, lon)


def _trip(
    trip_id: str,
    stops: list[tuple[str, int, int]],
    *,
    route_id: str = "R1",
    can_board_all: bool = True,
    can_alight_all: bool = True,
) -> Trip:
    return Trip(
        key=(A, trip_id),
        trip_id=trip_id,
        agency=A,
        route_id=route_id,
        service_id="3",
        headsign=None,
        stops=tuple(
            TripStop(
                stop=(A, stop_id),
                stop_sequence=index + 1,
                arrival=arrival,
                departure=departure,
                can_board=can_board_all,
                can_alight=can_alight_all,
            )
            for index, (stop_id, arrival, departure) in enumerate(stops)
        ),
    )


def _timetable(trips: list[Trip], transfers: tuple[TransferLink, ...] = ()) -> Timetable:
    stop_ids = {trip_stop.stop[1] for trip in trips for trip_stop in trip.stops}
    for link in transfers:
        stop_ids.update({link.from_stop[1], link.to_stop[1]})
    return Timetable(
        base_date=THURSDAY,
        stops={(A, stop_id): _stop(stop_id) for stop_id in stop_ids},
        routes={(A, "R1"): Route((A, "R1"), "1", "Test", None)},
        instances=tuple(TripInstance(trip, THURSDAY, 0) for trip in trips),
        transfers=transfers,
    )


HOUR = 3600


@pytest.fixture
def simple_graph():
    """One trip: X 08:00 -> Y 08:10 -> Z 08:25."""
    trip = _trip(
        "T1",
        [
            ("X", 8 * HOUR, 8 * HOUR),
            ("Y", 8 * HOUR + 600, 8 * HOUR + 620),
            ("Z", 8 * HOUR + 1500, 8 * HOUR + 1500),
        ],
    )
    return build_graph(
        _timetable([trip]), start_time=7 * HOUR, horizon_seconds=4 * HOUR, origin=(A, "X")
    )


class TestDagInvariant:
    def test_every_edge_points_forward_in_time(self, simple_graph) -> None:
        for node, targets in enumerate(simple_graph.adjacency):
            for target, kind in targets:
                assert simple_graph.node_time[target] >= simple_graph.node_time[node], (
                    f"{EdgeKind(kind).name} edge goes backwards in time"
                )

    def test_graph_is_acyclic(self, simple_graph) -> None:
        """Follows from the time ordering, but cheap to confirm directly."""
        colour = [0] * simple_graph.node_count  # 0 unvisited, 1 in progress, 2 done
        for root in range(simple_graph.node_count):
            if colour[root]:
                continue
            stack = [(root, iter(simple_graph.adjacency[root]))]
            colour[root] = 1
            while stack:
                node, targets = stack[-1]
                advanced = False
                for target, _ in targets:
                    assert colour[target] != 1, "cycle detected"
                    if colour[target] == 0:
                        colour[target] = 1
                        stack.append((target, iter(simple_graph.adjacency[target])))
                        advanced = True
                        break
                if not advanced:
                    colour[node] = 2
                    stack.pop()


class TestStructure:
    def test_ride_edges_link_consecutive_stops_of_a_trip(self, simple_graph) -> None:
        rides = [
            (node, target)
            for node, targets in enumerate(simple_graph.adjacency)
            for target, kind in targets
            if kind == EdgeKind.RIDE
        ]

        assert len(rides) == 2  # X->Y and Y->Z
        for node, target in rides:
            assert simple_graph.node_kind[node] == NodeKind.DEPARTURE
            assert simple_graph.node_kind[target] == NodeKind.ARRIVAL
            assert simple_graph.node_index[target] == simple_graph.node_index[node] + 1

    def test_alighting_costs_the_transfer_floor(self, simple_graph) -> None:
        alights = [
            (node, target)
            for node, targets in enumerate(simple_graph.adjacency)
            for target, kind in targets
            if kind == EdgeKind.ALIGHT
        ]

        assert alights
        for node, target in alights:
            assert (
                simple_graph.node_time[target] - simple_graph.node_time[node]
                == MIN_TRANSFER_SECONDS
            )

    def test_boarding_is_free_and_exactly_timed(self, simple_graph) -> None:
        """The wait chain already carried the rider to the departure's time."""
        boards = [
            (node, target)
            for node, targets in enumerate(simple_graph.adjacency)
            for target, kind in targets
            if kind == EdgeKind.BOARD
        ]

        assert boards
        for node, target in boards:
            assert simple_graph.node_time[target] == simple_graph.node_time[node]
            assert simple_graph.node_kind[target] == NodeKind.DEPARTURE

    def test_wait_chain_at_a_stop_is_strictly_increasing(self, simple_graph) -> None:
        for nodes in simple_graph.transfer_nodes.values():
            times = [simple_graph.node_time[node] for node in nodes]
            assert times == sorted(times)
            assert len(set(times)) == len(times)

    def test_origin_gets_a_platform_node_at_the_query_time(self) -> None:
        """So a rider present at 08:00 can board a bus leaving at 08:00."""
        trip = _trip("T1", [("X", 8 * HOUR, 8 * HOUR), ("Y", 8 * HOUR + 600, 8 * HOUR + 600)])
        graph = build_graph(
            _timetable([trip]),
            start_time=8 * HOUR,
            horizon_seconds=2 * HOUR,
            origin=(A, "X"),
        )

        entry = graph.entry_node((A, "X"), 8 * HOUR)

        assert entry is not None
        assert graph.node_time[entry] == 8 * HOUR
        assert any(kind == EdgeKind.BOARD for _, kind in graph.adjacency[entry])


class TestBoardingRestrictions:
    def test_a_no_pickup_stop_gets_no_board_edge(self) -> None:
        trip = _trip(
            "T1",
            [("X", 8 * HOUR, 8 * HOUR), ("Y", 8 * HOUR + 600, 8 * HOUR + 600)],
            can_board_all=False,
        )
        graph = build_graph(_timetable([trip]), start_time=7 * HOUR, horizon_seconds=3 * HOUR)

        boards = [
            kind
            for targets in graph.adjacency
            for _, kind in targets
            if kind == EdgeKind.BOARD
        ]

        assert boards == []

    def test_a_no_drop_off_stop_gets_no_alight_edge(self) -> None:
        trip = _trip(
            "T1",
            [("X", 8 * HOUR, 8 * HOUR), ("Y", 8 * HOUR + 600, 8 * HOUR + 600)],
            can_alight_all=False,
        )
        graph = build_graph(_timetable([trip]), start_time=7 * HOUR, horizon_seconds=3 * HOUR)

        alights = [
            kind
            for targets in graph.adjacency
            for _, kind in targets
            if kind == EdgeKind.ALIGHT
        ]

        assert alights == []

    def test_the_first_stop_is_never_an_alighting_point(self, simple_graph) -> None:
        for node, targets in enumerate(simple_graph.adjacency):
            for _, kind in targets:
                if kind == EdgeKind.ALIGHT:
                    assert simple_graph.node_index[node] > 0


class TestDeclaredTransfers:
    def test_walk_edge_uses_the_effective_transfer_time(self) -> None:
        trip_in = _trip("IN", [("W", 8 * HOUR, 8 * HOUR), ("X", 8 * HOUR + 600, 8 * HOUR + 600)])
        trip_out = _trip(
            "OUT", [("Y", 9 * HOUR, 9 * HOUR), ("Z", 9 * HOUR + 600, 9 * HOUR + 600)]
        )
        link = TransferLink(
            from_stop=(A, "X"),
            to_stop=(A, "Y"),
            seconds=180,
            declared_seconds=10,
            distance_metres=71.0,
        )
        graph = build_graph(
            _timetable([trip_in, trip_out], transfers=(link,)),
            start_time=7 * HOUR,
            horizon_seconds=4 * HOUR,
        )

        walks = [
            (node, target)
            for node, targets in enumerate(graph.adjacency)
            for target, kind in targets
            if kind == EdgeKind.WALK
        ]

        assert walks
        for node, target in walks:
            assert graph.node_time[target] - graph.node_time[node] == 180
            assert graph.node_stop[node] == (A, "X")
            assert graph.node_stop[target] == (A, "Y")


@pytest.fixture(scope="module")
def real_graph(db_engine: Engine):
    from a2transit.routing.timetable import build_timetable

    for agency, filename in (
        (AgencySource.THERIDE, "theride.zip"),
        (AgencySource.MBUS, "mbus.zip"),
    ):
        path = DATA_DIR / filename
        if not path.exists():
            pytest.skip(f"{path} not present; run `python -m a2transit.ingest`")
        load_from_path(db_engine, agency, path)

    timetable = build_timetable(db_engine, THURSDAY)
    return build_graph(timetable, start_time=8 * HOUR, horizon_seconds=6 * HOUR)


@pytest.mark.db
class TestAgainstRealFeeds:
    def test_every_edge_points_forward_in_time(self, real_graph) -> None:
        """The DAG invariant, on 116k nodes of real timetable."""
        for node, targets in enumerate(real_graph.adjacency):
            time = real_graph.node_time[node]
            for target, kind in targets:
                assert real_graph.node_time[target] >= time, EdgeKind(kind).name

    def test_graph_is_substantial_but_bounded(self, real_graph) -> None:
        assert 50_000 < real_graph.node_count < 500_000
        assert real_graph.edge_count > real_graph.node_count

    def test_no_node_falls_outside_the_window(self, real_graph) -> None:
        for node in range(real_graph.node_count):
            if real_graph.node_kind[node] == NodeKind.TRANSFER:
                assert real_graph.start_time <= real_graph.node_time[node] <= real_graph.end_time
