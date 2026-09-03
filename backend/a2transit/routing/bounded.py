"""Earliest arrival subject to a limit on how many vehicles may be boarded.

This exists to give the fewest-transfers criterion an oracle. M2's plain
Dijkstra answers only "earliest arrival", so checking RAPTOR's Pareto set
against it verifies exactly one entry — the fastest. Every other entry, which is
to say the entire second criterion, would be checked only against RAPTOR itself.

So M2's search is extended rather than reimplemented: the label becomes
(node, vehicles boarded so far) over the same time-expanded DAG, and boarding
edges are the ones that advance the counter. The state space is roughly
116,000 x 7, which is slow — the point is that it reaches the answer by a
different route than RAPTOR does. RAPTOR works in rounds over patterns; this
works by constrained shortest path over an event graph. Agreement between them
is evidence; agreement between RAPTOR and itself is not.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from heapq import heappop, heappush

from sqlalchemy import Engine

from a2transit.routing.graph import (
    DEFAULT_HORIZON_SECONDS,
    EdgeKind,
    TimeExpandedGraph,
    build_graph,
)
from a2transit.routing.models import Itinerary
from a2transit.routing.search import PlanningError, _path_to_legs
from a2transit.routing.timetable import StopKey, Timetable, build_timetable

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BoundedResult:
    max_boardings: int
    arrival: dt.datetime | None
    itinerary: Itinerary | None
    states_settled: int

    @property
    def found(self) -> bool:
        return self.arrival is not None


def _search_bounded(
    graph: TimeExpandedGraph,
    source: int,
    destination_stop: StopKey,
    max_boardings: int,
) -> tuple[int | None, list[int], int]:
    """Dijkstra over (node, boardings) keyed on node time.

    Returns (target state, parent-state array, states settled). A state is
    encoded as node * (max_boardings + 1) + boardings so the arrays stay flat.
    """
    width = max_boardings + 1
    state_count = graph.node_count * width

    parents: list[int] = [-1] * state_count
    settled = bytearray(state_count)
    targets = set(graph.arrival_nodes.get(destination_stop, ()))

    start_state = source * width
    queue: list[tuple[int, int]] = [(graph.node_time[source], start_state)]
    settled_count = 0

    while queue:
        _, state = heappop(queue)
        if settled[state]:
            continue
        settled[state] = 1
        settled_count += 1

        node, boardings = divmod(state, width)
        if node in targets:
            return state, parents, settled_count

        for target_node, kind in graph.adjacency[node]:
            # Boarding is the only edge that consumes one of the rider's
            # permitted vehicles; riding, waiting, alighting and walking do not.
            next_boardings = boardings + 1 if kind == EdgeKind.BOARD else boardings
            if next_boardings > max_boardings:
                continue
            next_state = target_node * width + next_boardings
            if settled[next_state]:
                continue
            if parents[next_state] == -1:
                parents[next_state] = state
            heappush(queue, (graph.node_time[target_node], next_state))

    return None, parents, settled_count


def _reconstruct_states(target: int, parents: list[int], width: int) -> list[int]:
    path: list[int] = []
    state = target
    while state != -1:
        path.append(state // width)
        state = parents[state]
    path.reverse()
    return path


def plan_bounded(
    graph: TimeExpandedGraph,
    timetable: Timetable,
    origin: StopKey,
    destination: StopKey,
    departure_seconds: int,
    max_boardings: int,
) -> BoundedResult:
    """Earliest arrival using at most `max_boardings` vehicles."""
    if origin not in timetable.stops:
        raise PlanningError(f"unknown origin stop {origin}")
    if destination not in timetable.stops:
        raise PlanningError(f"unknown destination stop {destination}")

    if max_boardings < 1:
        return BoundedResult(max_boardings, None, None, 0)

    source = graph.entry_node(origin, departure_seconds)
    if source is None:
        return BoundedResult(max_boardings, None, None, 0)

    target, parents, settled = _search_bounded(graph, source, destination, max_boardings)
    if target is None:
        return BoundedResult(max_boardings, None, None, settled)

    width = max_boardings + 1
    path = _reconstruct_states(target, parents, width)
    legs = _path_to_legs(graph, timetable, path)
    itinerary = Itinerary(
        origin=timetable.stops[origin],
        destination=timetable.stops[destination],
        requested_departure=timetable.absolute_time(departure_seconds),
        legs=legs,
    )
    return BoundedResult(
        max_boardings=max_boardings,
        arrival=timetable.absolute_time(graph.node_time[target // width]),
        itinerary=itinerary,
        states_settled=settled,
    )


def bounded_curve(
    db_engine: Engine,
    origin: StopKey,
    destination: StopKey,
    departure: dt.datetime,
    *,
    max_boardings: int = 6,
    horizon_seconds: int = DEFAULT_HORIZON_SECONDS,
    timetable: Timetable | None = None,
) -> tuple[BoundedResult, ...]:
    """The earliest arrival for every vehicle budget from 1 to `max_boardings`.

    This is the curve RAPTOR's Pareto set claims to describe, derived
    independently. Non-increasing by construction — a larger budget can only
    help — which is itself worth asserting.
    """
    timetable = timetable or build_timetable(db_engine, departure.date())
    departure_seconds = int(
        (departure - dt.datetime.combine(timetable.base_date, dt.time())).total_seconds()
    )
    graph = build_graph(
        timetable,
        start_time=departure_seconds,
        horizon_seconds=horizon_seconds,
        origin=origin,
    )
    return tuple(
        plan_bounded(graph, timetable, origin, destination, departure_seconds, budget)
        for budget in range(1, max_boardings + 1)
    )
