"""One entry point over both routing engines.

M2's Dijkstra and M3's RAPTOR answer different questions and are useful for
different reasons, so callers should not have to know which they are holding:

    plan_itineraries(...)              # RAPTOR, the Pareto set
    plan_itineraries(..., engine_name="dijkstra")   # the M2 reference

Both return `Itinerary` objects with the same leg structure, which is what makes
the differential test in test_differential.py a comparison of answers rather
than a comparison of two data models.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import Engine

from a2transit.routing.models import Itinerary, Leg, RideLeg, TransferLeg
from a2transit.routing.patterns import RaptorTimetable, build_raptor_timetable
from a2transit.routing.raptor import (
    DEFAULT_MAX_ROUNDS,
    ParetoEntry,
    RideStep,
    TransferStep,
    fewest_transfers_arriving_by,
    pareto_set,
    run_raptor,
)
from a2transit.routing.search import plan as dijkstra_plan
from a2transit.routing.timetable import StopKey, Timetable

logger = logging.getLogger(__name__)

EngineName = Literal["raptor", "dijkstra"]


@dataclass(frozen=True, slots=True)
class PlanOutcome:
    """Every non-dominated option, fewest vehicles first."""

    itineraries: tuple[Itinerary, ...]
    engine_name: EngineName
    seconds: float
    #: What live predictions did to the timetable this was planned on, or None
    #: when it was planned on the schedule. Typed loosely to keep the routing
    #: package from importing the realtime one — the dependency runs the other
    #: way, and should.
    delays: object | None = None

    @property
    def is_realtime(self) -> bool:
        return self.delays is not None

    @property
    def fastest(self) -> Itinerary | None:
        """The earliest-arriving option — the last entry, by Pareto ordering."""
        return self.itineraries[-1] if self.itineraries else None

    @property
    def fewest_transfers(self) -> Itinerary | None:
        """The option using fewest vehicles, whatever time it arrives."""
        return self.itineraries[0] if self.itineraries else None

    def arriving_by(self, deadline: dt.datetime) -> Itinerary | None:
        """Of the options arriving by `deadline`, the one with fewest transfers.

        Distinct from both properties above, and the question a rider with an
        appointment actually asks. Because the set runs from fewest vehicles to
        earliest arrival, the first entry meeting the deadline is the answer.
        """
        for itinerary in self.itineraries:
            if itinerary.arrival <= deadline:
                return itinerary
        return None


def _entry_to_itinerary(
    timetable: RaptorTimetable,
    entry: ParetoEntry,
    origin: StopKey,
    destination: StopKey,
    requested_departure: dt.datetime,
) -> Itinerary:
    """Turn RAPTOR steps into the shared leg model.

    Legs are derived from the ride steps alone, with a transfer leg filling each
    gap between one alighting and the next boarding. The RAPTOR TransferStep
    records when the rider *could* board, which is usually earlier than when
    they actually do; rendering that as the leg would show a transfer ending
    before the bus it connects to. Deriving from the gap also guarantees the
    legs chain, rather than merely tending to.
    """
    rides = [step for step in entry.steps if isinstance(step, RideStep)]
    legs: list[Leg] = []

    # A journey with no ride at all: the destination was within walking
    # distance and RAPTOR answered it in round 0.
    if not rides:
        walk = next((step for step in entry.steps if isinstance(step, TransferStep)), None)
        if walk is not None:
            legs.append(
                TransferLeg(
                    from_stop=timetable.stops[origin],
                    to_stop=timetable.stops[destination],
                    depart=requested_departure,
                    arrive=timetable.absolute_time(walk.arrive),
                )
            )
        return Itinerary(
            origin=timetable.stops[origin],
            destination=timetable.stops[destination],
            requested_departure=requested_departure,
            legs=tuple(legs),
        )

    # A journey may begin by walking away from the origin, in which case the
    # first ride boards somewhere else and the legs would not chain back to
    # where the rider actually started.
    if rides[0].board_stop != origin:
        legs.append(
            TransferLeg(
                from_stop=timetable.stops[origin],
                to_stop=timetable.stops[rides[0].board_stop],
                depart=requested_departure,
                arrive=timetable.absolute_time(rides[0].depart),
            )
        )

    for index, ride in enumerate(rides):
        if index > 0:
            previous = rides[index - 1]
            legs.append(
                TransferLeg(
                    from_stop=timetable.stops[previous.alight_stop],
                    to_stop=timetable.stops[ride.board_stop],
                    depart=timetable.absolute_time(previous.arrive),
                    arrive=timetable.absolute_time(ride.depart),
                )
            )
        route = timetable.routes.get((ride.agency, ride.route_id))
        legs.append(
            RideLeg(
                from_stop=timetable.stops[ride.board_stop],
                to_stop=timetable.stops[ride.alight_stop],
                depart=timetable.absolute_time(ride.depart),
                arrive=timetable.absolute_time(ride.arrive),
                agency=ride.agency,  # type: ignore[arg-type]
                route_id=ride.route_id,
                route_label=route.label if route else ride.route_id,
                trip_id=ride.run.trip_id,
                headsign=None,
                intermediate_stops=max(ride.alight_position - ride.board_position - 1, 0),
            )
        )

    # ...and may end on foot, for the same reason: the last bus can drop the
    # rider at a different stop 100 m from where they are going.
    if rides[-1].alight_stop != destination:
        final_walk = entry.steps[-1]
        legs.append(
            TransferLeg(
                from_stop=timetable.stops[rides[-1].alight_stop],
                to_stop=timetable.stops[destination],
                depart=timetable.absolute_time(rides[-1].arrive),
                arrive=timetable.absolute_time(
                    final_walk.arrive
                    if isinstance(final_walk, TransferStep)
                    else rides[-1].arrive
                ),
            )
        )

    return Itinerary(
        origin=timetable.stops[origin],
        destination=timetable.stops[destination],
        requested_departure=requested_departure,
        legs=tuple(legs),
    )


def plan_with_raptor(
    timetable: RaptorTimetable,
    origin: StopKey,
    destination: StopKey,
    departure: dt.datetime,
    *,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
) -> PlanOutcome:
    departure_seconds = int(
        (departure - dt.datetime.combine(timetable.base_date, dt.time())).total_seconds()
    )

    started = time.perf_counter()
    result = run_raptor(
        timetable,
        origin,
        departure_seconds,
        destination=destination,
        max_rounds=max_rounds,
    )
    entries = pareto_set(result, destination)
    elapsed = time.perf_counter() - started

    itineraries = tuple(
        _entry_to_itinerary(timetable, entry, origin, destination, departure)
        for entry in entries
    )
    return PlanOutcome(itineraries=itineraries, engine_name="raptor", seconds=elapsed)


def plan_itineraries(
    db_engine: Engine,
    origin: StopKey,
    destination: StopKey,
    departure: dt.datetime,
    *,
    engine_name: EngineName = "raptor",
    raptor_timetable: RaptorTimetable | None = None,
    dijkstra_timetable: Timetable | None = None,
) -> PlanOutcome:
    """Plan with either engine, returning the same shape from both."""
    if engine_name == "raptor":
        timetable = raptor_timetable or build_raptor_timetable(db_engine, departure.date())
        return plan_with_raptor(timetable, origin, destination, departure)

    started = time.perf_counter()
    result = dijkstra_plan(
        db_engine, origin, destination, departure, timetable=dijkstra_timetable
    )
    elapsed = time.perf_counter() - started
    return PlanOutcome(
        itineraries=(result.itinerary,) if result.itinerary else (),
        engine_name="dijkstra",
        seconds=elapsed,
    )


__all__ = [
    "EngineName",
    "PlanOutcome",
    "fewest_transfers_arriving_by",
    "plan_itineraries",
    "plan_with_raptor",
]
