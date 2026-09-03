"""Earliest-arrival search over the time-expanded graph.

Because every node carries its own time and every edge points forward, the cost
of reaching a node is simply that node's time — there is nothing to accumulate.
So this is Dijkstra with the node's own time as its key, which visits nodes in
increasing time order and can stop the moment it pops an arrival at the
destination. The first destination arrival popped is the earliest possible one.

This is the M2 reference implementation. It is not fast and is not meant to be;
its job is to be obviously right, so that M3's RAPTOR has something trustworthy
to be checked against.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from heapq import heappop, heappush

from sqlalchemy import Engine

from a2transit.db.models import AgencySource
from a2transit.routing.graph import (
    DEFAULT_HORIZON_SECONDS,
    EdgeKind,
    NodeKind,
    TimeExpandedGraph,
    build_graph,
)
from a2transit.routing.models import Itinerary, Leg, RideLeg, TransferLeg
from a2transit.routing.timetable import StopKey, Timetable, build_timetable

logger = logging.getLogger(__name__)


class PlanningError(Exception):
    """The query itself is unusable — an unknown stop, say."""


@dataclass(frozen=True, slots=True)
class SearchStats:
    nodes_settled: int
    nodes_total: int
    edges_total: int

    def __str__(self) -> str:
        return (
            f"settled {self.nodes_settled:,} of {self.nodes_total:,} nodes "
            f"({self.edges_total:,} edges)"
        )


def _seconds_since_midnight(moment: dt.datetime, base_date: dt.date) -> int:
    delta = moment - dt.datetime.combine(base_date, dt.time())
    return int(delta.total_seconds())


def _search(
    graph: TimeExpandedGraph, source: int, destination_stop: StopKey
) -> tuple[int | None, list[int], SearchStats]:
    """Dijkstra keyed on node time. Returns (target node, parents, stats)."""
    parents: list[int] = [-1] * graph.node_count
    settled = bytearray(graph.node_count)

    # Any arrival at the destination where the rider is allowed to get off.
    targets = set(graph.arrival_nodes.get(destination_stop, ()))

    queue: list[tuple[int, int]] = [(graph.node_time[source], source)]
    settled_count = 0

    while queue:
        _, node = heappop(queue)
        if settled[node]:
            continue
        settled[node] = 1
        settled_count += 1

        if node in targets:
            return (
                node,
                parents,
                SearchStats(settled_count, graph.node_count, graph.edge_count),
            )

        for target, _kind in graph.adjacency[node]:
            if not settled[target]:
                # First time we reach a node is via the earliest path, because
                # nodes come off the queue in time order and a node's time is
                # fixed. Later arrivals cannot improve on it.
                if parents[target] == -1:
                    parents[target] = node
                heappush(queue, (graph.node_time[target], target))

    return None, parents, SearchStats(settled_count, graph.node_count, graph.edge_count)


def _reconstruct(graph: TimeExpandedGraph, target: int, parents: list[int]) -> list[int]:
    path = [target]
    node = target
    while parents[node] != -1:
        node = parents[node]
        path.append(node)
    path.reverse()
    return path


def _path_to_legs(
    graph: TimeExpandedGraph, timetable: Timetable, path: list[int]
) -> tuple[Leg, ...]:
    """Collapse a node path into rider-visible legs.

    The path alternates between platform stretches and vehicle stretches. A run
    of nodes on one trip instance becomes one RideLeg; the gap between alighting
    and the next boarding becomes one TransferLeg.
    """
    legs: list[Leg] = []

    boarded_at: int | None = None  # index into path where the current ride began
    last_alight_node: int | None = None

    def stop_of(node: int) -> object:
        return timetable.stops[graph.node_stop[node]]

    for position, node in enumerate(path):
        kind = graph.node_kind[node]

        if kind == NodeKind.DEPARTURE and boarded_at is None:
            boarded_at = position
            if last_alight_node is not None:
                legs.append(
                    TransferLeg(
                        from_stop=stop_of(last_alight_node),  # type: ignore[arg-type]
                        to_stop=stop_of(node),  # type: ignore[arg-type]
                        depart=timetable.absolute_time(graph.node_time[last_alight_node]),
                        arrive=timetable.absolute_time(graph.node_time[node]),
                    )
                )
                last_alight_node = None
            continue

        # A ride ends at the last node of the path, or at the alight that
        # follows it (the next node is a platform rather than another vehicle).
        if boarded_at is not None and kind == NodeKind.ARRIVAL:
            is_last = position == len(path) - 1
            leaves_vehicle = not is_last and graph.node_kind[path[position + 1]] not in (
                NodeKind.DEPARTURE,
                NodeKind.ARRIVAL,
            )
            if is_last or leaves_vehicle:
                start_node = path[boarded_at]
                instance = graph.instances[graph.node_instance[start_node]]
                trip = instance.trip
                route = timetable.routes.get((trip.agency, trip.route_id))
                legs.append(
                    RideLeg(
                        from_stop=stop_of(start_node),  # type: ignore[arg-type]
                        to_stop=stop_of(node),  # type: ignore[arg-type]
                        depart=timetable.absolute_time(graph.node_time[start_node]),
                        arrive=timetable.absolute_time(graph.node_time[node]),
                        agency=trip.agency,
                        route_id=trip.route_id,
                        route_label=route.label if route else trip.route_id,
                        trip_id=trip.trip_id,
                        headsign=trip.headsign,
                        intermediate_stops=max(
                            graph.node_index[node] - graph.node_index[start_node] - 1, 0
                        ),
                    )
                )
                boarded_at = None
                last_alight_node = node

    return tuple(legs)


@dataclass(frozen=True, slots=True)
class PlanResult:
    itinerary: Itinerary | None
    stats: SearchStats


def plan_on_graph(
    graph: TimeExpandedGraph,
    timetable: Timetable,
    origin: StopKey,
    destination: StopKey,
    departure_seconds: int,
) -> PlanResult:
    origin_stop = timetable.stops.get(origin)
    destination_stop = timetable.stops.get(destination)
    if origin_stop is None:
        raise PlanningError(f"unknown origin stop {origin}")
    if destination_stop is None:
        raise PlanningError(f"unknown destination stop {destination}")

    requested = timetable.absolute_time(departure_seconds)
    empty_stats = SearchStats(0, graph.node_count, graph.edge_count)

    if origin == destination:
        return PlanResult(
            Itinerary(origin_stop, destination_stop, requested, ()),
            empty_stats,
        )

    source = graph.entry_node(origin, departure_seconds)
    if source is None:
        return PlanResult(None, empty_stats)

    target, parents, stats = _search(graph, source, destination)
    if target is None:
        return PlanResult(None, stats)

    path = _reconstruct(graph, target, parents)
    legs = _path_to_legs(graph, timetable, path)
    return PlanResult(
        Itinerary(origin_stop, destination_stop, requested, legs),
        stats,
    )


def plan(
    engine: Engine,
    origin: tuple[AgencySource, str],
    destination: tuple[AgencySource, str],
    departure: dt.datetime,
    *,
    horizon_seconds: int = DEFAULT_HORIZON_SECONDS,
    timetable: Timetable | None = None,
) -> PlanResult:
    """Earliest arrival from `origin` to `destination`, leaving at `departure`.

    Pass a prebuilt `timetable` to reuse it across queries on the same date;
    building one costs about half a second.
    """
    base_date = departure.date()
    timetable = timetable if timetable is not None else build_timetable(engine, base_date)
    departure_seconds = _seconds_since_midnight(departure, timetable.base_date)

    graph = build_graph(
        timetable,
        start_time=departure_seconds,
        horizon_seconds=horizon_seconds,
        origin=origin,
    )
    return plan_on_graph(graph, timetable, origin, destination, departure_seconds)


__all__ = [
    "EdgeKind",
    "PlanResult",
    "PlanningError",
    "SearchStats",
    "plan",
    "plan_on_graph",
]
