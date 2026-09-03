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
#: The feeds cannot be trusted here. All 17 of TheRide's declared transfers say
#: min_transfer_time = 10 s, but the Ypsilanti Transit Center bays they connect
#: are up to 71 m apart — 7.1 m/s, or 25 km/h on foot. Honouring that literally
#: produces itineraries nobody can actually make.
MIN_TRANSFER_SECONDS = 60

#: Walking speed used to sanity-check declared transfer times, in metres/second.
#: 1.3 m/s (~4.7 km/h) is the usual planning figure for an unhurried adult.
WALKING_SPEED_MPS = 1.3


def effective_transfer_seconds(
    declared_seconds: int | None = None,
    distance_metres: float | None = None,
) -> int:
    """How long a transfer really takes.

        max(declared, distance / walking speed, MIN_TRANSFER_SECONDS)

    Taking the maximum rather than trusting the feed means a transfer is only
    used when it is physically possible *and* clears the floor.

    The cost is real and is a known M2 limitation: TheRide runs timed pulses at
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
        candidates.append(int(-(-distance_metres // WALKING_SPEED_MPS)))  # ceil
    return max(candidates)
