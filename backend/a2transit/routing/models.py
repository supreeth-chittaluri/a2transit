"""Itineraries: what a plan returns.

Times here are real `datetime`s. Everything inside the engine works in seconds
since the query date's midnight — which is what lets a trip run past 24:00:00
without wrapping — but nothing outside should have to know that, so the
conversion happens at this boundary and only here.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum

from a2transit.db.models import AgencySource
from a2transit.routing.timetable import Stop


class LegKind(StrEnum):
    RIDE = "ride"
    TRANSFER = "transfer"


@dataclass(frozen=True, slots=True)
class RideLeg:
    """A leg spent aboard one vehicle."""

    from_stop: Stop
    to_stop: Stop
    depart: dt.datetime
    arrive: dt.datetime
    agency: AgencySource
    route_id: str
    route_label: str
    trip_id: str
    headsign: str | None
    #: Stops passed through without alighting, excluding both endpoints.
    intermediate_stops: int

    kind: LegKind = LegKind.RIDE

    @property
    def duration(self) -> dt.timedelta:
        return self.arrive - self.depart


@dataclass(frozen=True, slots=True)
class TransferLeg:
    """A leg spent on foot, or waiting to board at the stop just alighted at.

    A same-stop transfer has from_stop == to_stop: the rider has not moved, but
    the minimum connection time still applies.
    """

    from_stop: Stop
    to_stop: Stop
    depart: dt.datetime
    arrive: dt.datetime
    distance_metres: float | None = None

    kind: LegKind = LegKind.TRANSFER

    @property
    def duration(self) -> dt.timedelta:
        return self.arrive - self.depart

    @property
    def is_same_stop(self) -> bool:
        return self.from_stop.key == self.to_stop.key


Leg = RideLeg | TransferLeg


@dataclass(frozen=True, slots=True)
class Itinerary:
    """One journey. `legs` is empty when origin and destination are the same."""

    origin: Stop
    destination: Stop
    #: When the rider is ready to leave — the query time, not the first departure.
    requested_departure: dt.datetime
    legs: tuple[Leg, ...]

    @property
    def departure(self) -> dt.datetime:
        return self.legs[0].depart if self.legs else self.requested_departure

    @property
    def arrival(self) -> dt.datetime:
        return self.legs[-1].arrive if self.legs else self.requested_departure

    @property
    def duration(self) -> dt.timedelta:
        """Door to door, counting the initial wait at the origin stop."""
        return self.arrival - self.requested_departure

    @property
    def ride_legs(self) -> tuple[RideLeg, ...]:
        return tuple(leg for leg in self.legs if isinstance(leg, RideLeg))

    @property
    def transfer_count(self) -> int:
        """Vehicle changes — one fewer than the number of vehicles boarded."""
        return max(len(self.ride_legs) - 1, 0)

    @property
    def initial_wait(self) -> dt.timedelta:
        return self.departure - self.requested_departure

    def describe(self) -> str:
        """A compact human-readable rendering, for the CLI and for test failures."""
        if not self.legs:
            return f"{self.origin.label}: already at destination"

        lines = [
            f"{self.origin.name} -> {self.destination.name}",
            f"  depart {self.departure:%a %Y-%m-%d %H:%M}"
            f"  arrive {self.arrival:%H:%M}"
            f"  ({self.duration.total_seconds() / 60:.0f} min,"
            f" {self.transfer_count} transfer{'s' if self.transfer_count != 1 else ''})",
        ]
        for leg in self.legs:
            if isinstance(leg, RideLeg):
                lines.append(
                    f"    {leg.depart:%H:%M} {leg.from_stop.name}"
                    f"  --[{leg.agency.value} {leg.route_label}"
                    f"{' to ' + leg.headsign if leg.headsign else ''}]-->"
                )
                lines.append(f"    {leg.arrive:%H:%M} {leg.to_stop.name}")
            else:
                # A transfer leg spans the walk *and* the wait for the next
                # vehicle, so it is never described as walking time.
                where = "wait here" if leg.is_same_stop else f"transfer to {leg.to_stop.name}"
                lines.append(
                    f"    {leg.depart:%H:%M} {where}"
                    f" ({leg.duration.total_seconds() / 60:.0f} min incl. wait)"
                )
        return "\n".join(lines)
