"""Time-expanded graph over a timetable.

Every node carries a time, and every edge points forward in time. That makes the
graph a DAG whose topological order is simply time order, so earliest-arrival
needs no argument about why it is correct: relax nodes in time order and the
first label you assign to a node is final.

That is the whole reason M2 uses this formulation rather than the smaller,
faster time-dependent one (nodes = stops, relax by "next departure after t").
The time-dependent version is what production planners use, but its correctness
leans on the FIFO/non-overtaking property and on never boarding a trip twice —
exactly the kind of cleverness you do not want in the oracle that M3's RAPTOR
will be checked against.

Node kinds
----------
ARR(instance, i)   a vehicle has arrived at its i-th stop
DEP(instance, i)   a vehicle is about to leave its i-th stop
XFER(stop, t)      a rider is standing at `stop` at time t, able to board

Edges
-----
ARR -> DEP         stay on board through a stop
DEP -> ARR         ride to the next stop
ARR -> XFER        alight, arriving at the platform one transfer time later
XFER -> XFER       wait for the next event at this stop
XFER -> XFER       walk a footpath to a nearby stop
XFER -> DEP        board a vehicle leaving at exactly this time

Alighting costs a transfer time, boarding costs nothing: the wait chain already
carries the rider forward to the departure's own time, so charging both would
double-count.

WALK edges do not create nodes. They point at the first platform node at or
after the walk lands, because landing is only ever a prelude to boarding and
every departure is already a platform time. Pass 4 explains what that buys and
what it costs.

Scope
-----
The graph is built for a time range rather than a whole day. A full three-date
window is ~219,000 stop_times, so materialising all of it would mean roughly
660,000 nodes for a query that will touch a fraction of them.

`horizon_seconds` bounds **boardings**, not arrivals. Platform nodes exist only
inside [start, start + horizon], so no vehicle can be boarded outside it — but a
trip boarded inside the window keeps its whole stop sequence, including stops
that fall beyond the horizon. Truncating mid-ride would delete a legitimate
journey at an arbitrary boundary and report "unreachable" for a bus the rider is
already sitting on. The consequence worth knowing: an itinerary's final arrival
may be later than start + horizon, though every boarding within it is not.
"""

from __future__ import annotations

import logging
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from enum import IntEnum

from a2transit.routing.constants import MIN_TRANSFER_SECONDS
from a2transit.routing.timetable import StopKey, Timetable, TripInstance

logger = logging.getLogger(__name__)

#: Six hours. Ann Arbor's network spans ~30 km; no sane itinerary inside it
#: takes longer, and a bounded build keeps the reference implementation usable.
DEFAULT_HORIZON_SECONDS = 6 * 3600


class NodeKind(IntEnum):
    ARRIVAL = 0
    DEPARTURE = 1
    TRANSFER = 2


class EdgeKind(IntEnum):
    RIDE = 0
    STAY_ON_BOARD = 1
    ALIGHT = 2
    WAIT = 3
    BOARD = 4
    WALK = 5


@dataclass(slots=True)
class TimeExpandedGraph:
    """Nodes and adjacency, stored as parallel arrays.

    Objects per node would cost more than the graph itself at this size; the
    arrays are indexed by node id throughout.
    """

    #: Time associated with each node, seconds since base-date midnight.
    node_time: list[int]
    node_kind: list[int]
    #: For ARR/DEP: index into `instances`. For XFER: -1.
    node_instance: list[int]
    #: For ARR/DEP: position within the trip. For XFER: -1.
    node_index: list[int]
    #: Stop each node sits at.
    node_stop: list[StopKey]

    #: adjacency[node] -> list of (target node, edge kind)
    adjacency: list[list[tuple[int, int]]]

    instances: tuple[TripInstance, ...]
    #: XFER nodes per stop, in ascending time order, for entry-point lookup.
    transfer_nodes: dict[StopKey, list[int]]
    #: ARR nodes per stop where alighting is permitted, in ascending time order.
    arrival_nodes: dict[StopKey, list[int]]

    start_time: int
    end_time: int

    @property
    def node_count(self) -> int:
        return len(self.node_time)

    @property
    def edge_count(self) -> int:
        return sum(len(targets) for targets in self.adjacency)

    def entry_node(self, stop: StopKey, at_or_after: int) -> int | None:
        """The XFER node a rider standing at `stop` from `at_or_after` reaches first."""
        nodes = self.transfer_nodes.get(stop)
        if not nodes:
            return None
        times = [self.node_time[node] for node in nodes]
        position = bisect_left(times, at_or_after)
        return nodes[position] if position < len(nodes) else None

    def __repr__(self) -> str:
        return (
            f"<TimeExpandedGraph nodes={self.node_count} edges={self.edge_count} "
            f"window={self.start_time}..{self.end_time}>"
        )


def build_graph(
    timetable: Timetable,
    *,
    start_time: int,
    horizon_seconds: int = DEFAULT_HORIZON_SECONDS,
    origin: StopKey | None = None,
    destination: StopKey | None = None,
) -> TimeExpandedGraph:
    """Materialise the graph for [start_time, start_time + horizon_seconds].

    `origin` adds a XFER node at exactly `start_time` there, so a rider who
    arrives at their origin stop at that moment can board a vehicle leaving at
    that moment without waiting for the next scheduled event.

    `destination` adds a node at the exact moment of every walk that ends there.
    Everywhere else a walk is snapped forward to the next platform time, which
    costs nothing because landing is only a prelude to boarding — but at the
    destination the landing *is* the answer, and rounding it up to the next bus
    would report a rider as arriving several minutes after they got there.
    """
    end_time = start_time + horizon_seconds

    node_time: list[int] = []
    node_kind: list[int] = []
    node_instance: list[int] = []
    node_index: list[int] = []
    node_stop: list[StopKey] = []
    adjacency: list[list[tuple[int, int]]] = []

    def add_node(time: int, kind: NodeKind, stop: StopKey, instance: int, index: int) -> int:
        node_time.append(time)
        node_kind.append(int(kind))
        node_instance.append(instance)
        node_index.append(index)
        node_stop.append(stop)
        adjacency.append([])
        return len(node_time) - 1

    # ------------------------------------------------------------------
    # Pass 1: vehicle nodes.
    #
    # An instance is included whole if any of its stops falls in the window —
    # taking only the stops inside would sever ride edges mid-trip and make
    # journeys that enter the window partway through disappear.
    # ------------------------------------------------------------------
    kept_instances: list[TripInstance] = []
    for instance in timetable.instances:
        last = len(instance.trip.stops) - 1
        if instance.arrival_at(last) < start_time or instance.departure_at(0) > end_time:
            continue
        kept_instances.append(instance)

    instances = tuple(kept_instances)

    #: alight times per stop, and departure nodes per (stop, time) for boarding
    alight_times: dict[StopKey, set[int]] = defaultdict(set)
    departures_at: dict[tuple[StopKey, int], list[int]] = defaultdict(list)

    for instance_index, instance in enumerate(instances):
        trip_stops = instance.trip.stops
        last = len(trip_stops) - 1

        arrival_ids: list[int] = []
        departure_ids: list[int] = []
        for index, trip_stop in enumerate(trip_stops):
            arrival_ids.append(
                add_node(
                    instance.arrival_at(index),
                    NodeKind.ARRIVAL,
                    trip_stop.stop,
                    instance_index,
                    index,
                )
            )
            departure_ids.append(
                add_node(
                    instance.departure_at(index),
                    NodeKind.DEPARTURE,
                    trip_stop.stop,
                    instance_index,
                    index,
                )
            )

        for index, trip_stop in enumerate(trip_stops):
            if index < last:
                # Stay aboard through this stop, then ride to the next one.
                adjacency[arrival_ids[index]].append(
                    (departure_ids[index], int(EdgeKind.STAY_ON_BOARD))
                )
                adjacency[departure_ids[index]].append(
                    (arrival_ids[index + 1], int(EdgeKind.RIDE))
                )
            if trip_stop.can_alight and index > 0:
                alight_times[trip_stop.stop].add(
                    instance.arrival_at(index) + MIN_TRANSFER_SECONDS
                )
            if trip_stop.can_board and index < last:
                departures_at[(trip_stop.stop, instance.departure_at(index))].append(
                    departure_ids[index]
                )

    # ------------------------------------------------------------------
    # Pass 2: platform (XFER) times per stop.
    #
    # A rider can be standing at a stop at: a time they alighted (plus the
    # transfer floor), a time a vehicle departs, or the query's own start time
    # at the origin.
    #
    # Walking in adds no time of its own, which is the whole reason M4's 8,308
    # footpaths do not overwhelm this graph — see the WALK pass below.
    # ------------------------------------------------------------------
    platform_times: dict[StopKey, set[int]] = defaultdict(set)
    for stop, times in alight_times.items():
        platform_times[stop].update(times)
    for (stop, time), _ in departures_at.items():
        platform_times[stop].add(time)
    if origin is not None:
        platform_times[origin].add(start_time)

    # Exact landing times for walks that end at the destination. Their sources
    # are the same ones pass 3 walks from: a vehicle arrival plus the floor, or
    # the origin.
    if destination is not None:
        for footpath in timetable.footpaths:
            if footpath.to_stop != destination:
                continue
            ready_times = set(alight_times.get(footpath.from_stop, ()))
            if footpath.from_stop == origin:
                ready_times.add(start_time)
            for ready_at in ready_times:
                landed = ready_at + footpath.seconds
                if start_time <= landed <= end_time:
                    platform_times[destination].add(landed)

    transfer_nodes: dict[StopKey, list[int]] = {}
    transfer_lookup: dict[tuple[StopKey, int], int] = {}
    for stop, times in platform_times.items():
        ordered = sorted(time for time in times if start_time <= time <= end_time)
        if not ordered:
            continue
        ids: list[int] = []
        for time in ordered:
            node = add_node(time, NodeKind.TRANSFER, stop, -1, -1)
            transfer_lookup[(stop, time)] = node
            ids.append(node)
        # Wait: stand at the stop until the next thing happens there.
        for earlier, later in zip(ids, ids[1:], strict=False):
            adjacency[earlier].append((later, int(EdgeKind.WAIT)))
        transfer_nodes[stop] = ids

    # ------------------------------------------------------------------
    # Pass 3: edges that join vehicles to platforms, and the footpaths out.
    #
    # A walk starts where RAPTOR starts one: at a vehicle arrival, or at the
    # origin. Nowhere else. Two consequences follow, and both are load-bearing.
    #
    # **It costs no nodes.** The obvious construction gives every walk its own
    # landing node at exactly t + w. With 8,308 footpaths at a mean out-degree
    # of 7.1 that is ~467,000 new nodes on a graph of 113,000 — the reference
    # implementation stops being usable and the 500-case differential stops
    # being something anyone runs. But landing is only ever a prelude to
    # boarding, and every departure is already a platform time, so the edge can
    # point at the first platform node at or after the walk lands. Same
    # journeys, no new nodes.
    #
    # **It cannot chain.** Snapping shares nodes between journeys, so if XFER
    # nodes originated walks a rider could arrive at X on foot, land on some
    # other rider's node, and walk on to Y — two hops, 800 m, and something
    # RAPTOR would never answer. Because only ARR nodes and the origin have
    # WALK edges, and an ARR node is reachable only by riding, a second walk
    # requires a bus in between. That is exactly RAPTOR's rule, enforced by the
    # shape of the graph rather than by remembering to enforce it.
    #
    # The floor is charged once, on getting off, then the walk on top:
    # arrival + MIN_TRANSFER_SECONDS + footpath.seconds. Identical arithmetic to
    # raptor.run_raptor, which is the point.
    #
    # What snapping deliberately loses is the exact moment a walk *ends*, which
    # only matters when it ends where the rider was going.
    # ------------------------------------------------------------------
    platform_times_at: dict[StopKey, list[int]] = {
        stop: [node_time[node] for node in nodes] for stop, nodes in transfer_nodes.items()
    }
    footpaths_from: dict[StopKey, list[tuple[StopKey, int]]] = defaultdict(list)
    for footpath in timetable.footpaths:
        footpaths_from[footpath.from_stop].append((footpath.to_stop, footpath.seconds))

    def walk_edges_from(node: int, stop: StopKey, ready_at: int) -> int:
        """Link `node` to where a rider leaving `stop` at `ready_at` can walk."""
        added = 0
        for to_stop, seconds in footpaths_from.get(stop, ()):
            landed = ready_at + seconds
            if landed > end_time:
                continue
            candidates = transfer_nodes.get(to_stop)
            if not candidates:
                continue
            position = bisect_left(platform_times_at[to_stop], landed)
            if position < len(candidates):
                adjacency[node].append((candidates[position], int(EdgeKind.WALK)))
                added += 1
        return added

    walk_count = 0
    arrival_nodes: dict[StopKey, list[int]] = defaultdict(list)
    for node in range(len(node_time)):
        if node_kind[node] != int(NodeKind.ARRIVAL):
            continue
        instance = instances[node_instance[node]]
        index = node_index[node]
        trip_stop = instance.trip.stops[index]
        if not trip_stop.can_alight or index == 0:
            continue

        arrival_nodes[trip_stop.stop].append(node)
        ready_at = node_time[node] + MIN_TRANSFER_SECONDS
        platform = transfer_lookup.get((trip_stop.stop, ready_at))
        if platform is not None:
            adjacency[node].append((platform, int(EdgeKind.ALIGHT)))
        walk_count += walk_edges_from(node, trip_stop.stop, ready_at)

    for (stop, time), departure_ids in departures_at.items():
        platform = transfer_lookup.get((stop, time))
        if platform is None:
            continue
        for departure_id in departure_ids:
            adjacency[platform].append((departure_id, int(EdgeKind.BOARD)))

    # The origin is the one platform node that may walk. The rider is already
    # standing there rather than getting off something, so no floor is charged —
    # which is how RAPTOR seeds round 0 too.
    if origin is not None:
        origin_node = transfer_lookup.get((origin, start_time))
        if origin_node is not None:
            walk_count += walk_edges_from(origin_node, origin, start_time)

    logger.debug("%d walk edges from %d footpaths", walk_count, len(timetable.footpaths))

    for stop in arrival_nodes:
        arrival_nodes[stop].sort(key=lambda node: node_time[node])

    graph = TimeExpandedGraph(
        node_time=node_time,
        node_kind=node_kind,
        node_instance=node_instance,
        node_index=node_index,
        node_stop=node_stop,
        adjacency=adjacency,
        instances=instances,
        transfer_nodes=transfer_nodes,
        arrival_nodes=dict(arrival_nodes),
        start_time=start_time,
        end_time=end_time,
    )
    logger.debug("built %r", graph)
    return graph
