"""Constants shared across the routing engines.

M3's RAPTOR must agree with M2's Dijkstra reference on every answer, which is
only meaningful if both are working from the same numbers. Anything that changes
what a correct itinerary *is* belongs here rather than in either engine.
"""

from __future__ import annotations

#: Seconds in a service day. GTFS times count from service midnight and are
#: allowed to exceed this — MBus reaches 27:15:00 — so it is an offset between
#: adjacent service dates, never a modulus to wrap times by.
SECONDS_PER_DAY = 86_400

#: Floor on any transfer, in seconds, including boarding a second vehicle at the
#: stop you alighted at.
#:
#: The feeds cannot be trusted here. All 15 of TheRide's usable declared
#: transfers say min_transfer_time = 10 s, but the Ypsilanti Transit Center bays
#: they connect are up to 70.5 m apart — 7 m/s, or 25 km/h on foot. Honouring
#: that literally produces itineraries nobody can actually make.
MIN_TRANSFER_SECONDS = 60

#: Walking speed, in metres/second. 1.3 m/s (~4.7 km/h) is the usual planning
#: figure for an unhurried adult.
WALKING_SPEED_MPS = 1.3

#: How much longer a walked path is than the straight line between its ends.
#:
#: Distances here come from PostGIS ST_Distance over `geography`, which is the
#: great-circle distance — a line through buildings, across the Huron, and over
#: the fence at Michigan Stadium. Nobody walks that. 1.3 is the usual planning
#: allowance for a street grid, and is most of what routing the walk over OSRM
#: would recover. M4 keeps OSRM off the critical path deliberately: it is a
#: rate-limited demo server with no SLA, and a journey planner must not stop
#: working because someone else's free service is busy.
WALKING_DETOUR_FACTOR = 1.3

#: Furthest apart two stops may be and still be joined by a generated footpath.
#:
#: 400 m is about a five-minute walk, and the distance at which the two networks
#: genuinely touch: 728 TheRide/MBus stop pairs fall inside it, several under
#: 2 m. It is also the figure docs/feeds.md measured the cross-agency premise
#: at, so moving it invalidates that number rather than merely changing a knob.
FOOTPATH_MAX_METRES = 400.0


def walking_seconds(distance_metres: float) -> int:
    """Time to walk a straight-line distance, detour allowed for, rounded up.

    The two constants happen to cancel to exactly one second per metre today.
    That is arithmetic coincidence, not a shortcut worth relying on — change
    either figure and it stops holding.
    """
    return int(-(-distance_metres * WALKING_DETOUR_FACTOR // WALKING_SPEED_MPS))  # ceil


def effective_transfer_seconds(
    declared_seconds: int | None = None,
    distance_metres: float | None = None,
) -> int:
    """How long a transfer really takes.

        max(declared, walking_seconds(distance), MIN_TRANSFER_SECONDS)

    Taking the maximum rather than trusting the feed means a transfer is only
    used when it is physically possible *and* clears the floor.

    The cost is real and is a known limitation: TheRide runs timed pulses at
    Blake Transit Center where connecting buses are held for each other, and the
    floor rejects those. GTFS has no way for the agency to express a guaranteed
    timed transfer (that needs transfer_type=1 with the vehicles' own
    coordination, which neither feed publishes), so there is nothing in the data
    to distinguish a held connection from a coincidental one.
    """
    candidates = [MIN_TRANSFER_SECONDS]
    if declared_seconds is not None:
        candidates.append(declared_seconds)
    if distance_metres is not None:
        candidates.append(walking_seconds(distance_metres))
    return max(candidates)

