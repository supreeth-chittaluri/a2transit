"""Differential testing between the RAPTOR and Dijkstra engines.

Cases are generated from a fixed seed so a mismatch reproduces exactly. The
generator uses its own `random.Random` instance rather than the module-level
functions: seeding the global generator would make results depend on whatever
else in the process had drawn from it, which is precisely the property a
reproducible failure cannot afford.

Case N is the same case on every run, and the first N of a larger run are the
same cases as a smaller one, so a quick 60-case pass is a genuine prefix of the
full 500 rather than a different sample.
"""

from __future__ import annotations

import datetime as dt
import logging
import random
import time
from dataclasses import dataclass

from sqlalchemy import Engine, text

from a2transit.db.models import AgencySource
from a2transit.routing.engine import plan_with_raptor
from a2transit.routing.patterns import RaptorTimetable, build_raptor_timetable
from a2transit.routing.places import PlaceAttachment, with_places, with_places_raptor
from a2transit.routing.search import plan as dijkstra_plan
from a2transit.routing.timetable import StopKey, Timetable, build_timetable

logger = logging.getLogger(__name__)

#: Fixed for reproducibility. Change it only to deliberately resample.
SEED = 20260910

#: Spans an ordinary weekday, a holiday with reduced service, and a Saturday,
#: because the calendar is where the engines could most plausibly diverge.
DEFAULT_DATES = (
    dt.date(2026, 9, 10),  # Thursday, full service
    dt.date(2026, 9, 7),   # Labor Day, MBus down to 5 routes
    dt.date(2026, 9, 12),  # Saturday
)

DEFAULT_HOURS = (6, 8, 10, 12, 15, 17, 19, 22)
DEFAULT_MINUTES = (0, 15, 30, 45)


@dataclass(frozen=True, slots=True)
class Case:
    index: int
    origin: StopKey
    destination: StopKey
    departure: dt.datetime

    @property
    def reproduce(self) -> str:
        return (
            f"python -m a2transit.routing "
            f"--from {self.origin[0].value}:{self.origin[1]} "
            f"--to {self.destination[0].value}:{self.destination[1]} "
            f"--depart {self.departure.isoformat()} --compare"
        )

    def __str__(self) -> str:
        return (
            f"case {self.index}: {self.origin[0].value}:{self.origin[1]} -> "
            f"{self.destination[0].value}:{self.destination[1]} at {self.departure:%Y-%m-%d %H:%M}"
        )


def load_servable_stops(engine: Engine) -> tuple[StopKey, ...]:
    """Stops with at least one scheduled departure, in a deterministic order.

    Ordered explicitly: an unordered query may return rows in whatever order the
    planner chooses, which would make the seeded sample stop being reproducible.
    """
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT DISTINCT agency_source, stop_id
                  FROM stop_times
                 ORDER BY agency_source, stop_id
                """
            )
        ).all()
    return tuple((AgencySource(row.agency_source), row.stop_id) for row in rows)


def generate_cases(
    stops: tuple[StopKey, ...],
    count: int,
    *,
    dates: tuple[dt.date, ...] = DEFAULT_DATES,
    seed: int = SEED,
) -> tuple[Case, ...]:
    rng = random.Random(seed)
    cases: list[Case] = []
    for index in range(count):
        origin, destination = rng.sample(stops, 2)
        day = dates[index % len(dates)]
        departure = dt.datetime.combine(
            day, dt.time(rng.choice(DEFAULT_HOURS), rng.choice(DEFAULT_MINUTES))
        )
        cases.append(Case(index, origin, destination, departure))
    return tuple(cases)


@dataclass(frozen=True, slots=True)
class Comparison:
    case: Case
    raptor_arrival: dt.datetime | None
    dijkstra_arrival: dt.datetime | None
    raptor_trips: tuple[str, ...]
    dijkstra_trips: tuple[str, ...]
    raptor_seconds: float
    dijkstra_seconds: float

    @property
    def arrivals_agree(self) -> bool:
        return self.raptor_arrival == self.dijkstra_arrival

    @property
    def trips_agree(self) -> bool:
        return self.raptor_trips == self.dijkstra_trips

    def describe(self) -> str:
        return (
            f"{self.case}\n"
            f"  RAPTOR   {self.raptor_arrival}  trips={list(self.raptor_trips)}\n"
            f"  Dijkstra {self.dijkstra_arrival}  trips={list(self.dijkstra_trips)}\n"
            f"  reproduce: {self.case.reproduce}"
        )


@dataclass
class _DateContext:
    raptor: RaptorTimetable
    dijkstra: Timetable


#: Rounds allowed during comparison. Higher than the product default of 6,
#: because M2's Dijkstra has no round limit at all and will happily return a
#: nine-ride Saturday journey. Capping RAPTOR lower would make the engines
#: disagree by configuration rather than by correctness, hiding real mismatches
#: behind an expected one.
COMPARISON_MAX_ROUNDS = 12


def compare_cases(
    engine: Engine,
    cases: tuple[Case, ...],
    *,
    progress_every: int = 0,
    max_rounds: int = COMPARISON_MAX_ROUNDS,
    attachment: PlaceAttachment | None = None,
) -> tuple[Comparison, ...]:
    """Run every case through both engines. Timetables are built once per date.

    `attachment` puts the same synthetic origin and destination into both
    timetables, so a door-to-door query is differentially tested exactly like a
    stop-to-stop one. That it needs no other change is the argument for
    modelling a place as a stop rather than as an access layer per engine.
    """
    contexts: dict[dt.date, _DateContext] = {}
    comparisons: list[Comparison] = []

    for position, case in enumerate(cases, start=1):
        day = case.departure.date()
        if day not in contexts:
            raptor = build_raptor_timetable(engine, day)
            dijkstra = build_timetable(engine, day)
            if attachment is not None:
                raptor = with_places_raptor(raptor, attachment)
                dijkstra = with_places(dijkstra, attachment)
            contexts[day] = _DateContext(raptor=raptor, dijkstra=dijkstra)
        context = contexts[day]

        outcome = plan_with_raptor(
            context.raptor,
            case.origin,
            case.destination,
            case.departure,
            max_rounds=max_rounds,
        )
        fastest = outcome.fastest

        started = time.perf_counter()
        reference = dijkstra_plan(
            engine,
            case.origin,
            case.destination,
            case.departure,
            timetable=context.dijkstra,
        ).itinerary
        dijkstra_seconds = time.perf_counter() - started

        comparisons.append(
            Comparison(
                case=case,
                raptor_arrival=fastest.arrival if fastest else None,
                dijkstra_arrival=reference.arrival if reference else None,
                raptor_trips=tuple(leg.trip_id for leg in fastest.ride_legs)
                if fastest
                else (),
                dijkstra_trips=tuple(leg.trip_id for leg in reference.ride_legs)
                if reference
                else (),
                raptor_seconds=outcome.seconds,
                dijkstra_seconds=dijkstra_seconds,
            )
        )

        if progress_every and position % progress_every == 0:
            logger.info("compared %d/%d cases", position, len(cases))

    return tuple(comparisons)


@dataclass(frozen=True)
class ComparisonSummary:
    total: int
    both_found: int
    neither_found: int
    arrival_mismatches: tuple[Comparison, ...]
    trip_mismatches: tuple[Comparison, ...]
    raptor_times: tuple[float, ...]
    dijkstra_times: tuple[float, ...]

    def _percentile(self, values: tuple[float, ...], fraction: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        return ordered[min(int(fraction * len(ordered)), len(ordered) - 1)]

    def report(self) -> str:
        raptor_p50 = self._percentile(self.raptor_times, 0.5) * 1000
        raptor_p95 = self._percentile(self.raptor_times, 0.95) * 1000
        dijkstra_p50 = self._percentile(self.dijkstra_times, 0.5) * 1000
        dijkstra_p95 = self._percentile(self.dijkstra_times, 0.95) * 1000
        speedup = dijkstra_p50 / raptor_p50 if raptor_p50 else 0.0

        lines = [
            f"{self.total} cases: {self.both_found} routable, "
            f"{self.neither_found} unroutable by both",
            f"  arrival mismatches: {len(self.arrival_mismatches)}",
            f"  trip mismatches:    {len(self.trip_mismatches)}",
            "",
            f"{'engine':<12}{'p50 ms':>10}{'p95 ms':>10}",
            f"{'RAPTOR':<12}{raptor_p50:>10.2f}{raptor_p95:>10.2f}",
            f"{'Dijkstra':<12}{dijkstra_p50:>10.2f}{dijkstra_p95:>10.2f}",
            f"  RAPTOR is {speedup:.0f}x faster at p50",
        ]
        for mismatch in self.arrival_mismatches[:5]:
            lines.extend(["", mismatch.describe()])
        return "\n".join(lines)


def summarise(comparisons: tuple[Comparison, ...]) -> ComparisonSummary:
    return ComparisonSummary(
        total=len(comparisons),
        both_found=sum(
            1 for c in comparisons if c.raptor_arrival and c.dijkstra_arrival
        ),
        neither_found=sum(
            1
            for c in comparisons
            if c.raptor_arrival is None and c.dijkstra_arrival is None
        ),
        arrival_mismatches=tuple(c for c in comparisons if not c.arrivals_agree),
        # Ties can legitimately be broken differently, so trip-level
        # disagreement is reported but is not on its own a failure.
        trip_mismatches=tuple(
            c for c in comparisons if c.arrivals_agree and not c.trips_agree
        ),
        raptor_times=tuple(c.raptor_seconds for c in comparisons),
        dijkstra_times=tuple(c.dijkstra_seconds for c in comparisons),
    )
