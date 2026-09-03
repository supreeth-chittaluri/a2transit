"""Process-wide objects the routers share.

One timetable cache per process, not per request and not per router. Building a
timetable costs ~330 ms against a 4 ms query, so where it lives is the
difference between an API that answers in milliseconds and one that does not.
"""

from __future__ import annotations

from functools import lru_cache

from a2transit.db.session import get_engine
from a2transit.routing.service import TimetableCache


@lru_cache(maxsize=1)
def timetable_cache() -> TimetableCache:
    return TimetableCache(get_engine())
