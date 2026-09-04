"""Generate walkable stop-to-stop links from geometry.

This is the milestone where the two networks become one. The agencies between
them declare 15 usable transfers — every one of TheRide's, every one inside
Ypsilanti Transit Center, none crossing to MBus — so until this table exists a
TheRide-to-MBus query has no edge to cross and correctly returns nothing, even
where the two agencies' stops share a corner.

One PostGIS query over the GiST index on `stops.geog` finds every pair within
FOOTPATH_MAX_METRES and hands back the spheroid distance. Two things are worth
saying about that number:

* It is `geography`, not `geometry`, so ST_DWithin's radius is metres on the
  WGS84 spheroid and nothing has to be projected by hand. An earlier spherical
  haversine estimate put the cross-agency count at 732; four pairs sit close
  enough to 400 m that the spheroid moves them across it, so 728 is the number.
* It is a straight line. The detour allowance that turns it into a walking time
  lives in `routing.constants`, with the transfer floor, because the routing
  engines must agree on what a transfer costs and there must be exactly one
  place that decides.

Declared transfers are merged in rather than replaced: where an agency asserts
a pair, its `min_transfer_time` is carried through `effective_transfer_seconds`
alongside the distance, and a declared pair beyond the radius is kept even
though geometry alone would not have produced it. Neither feed has one today —
the longest declared transfer is 70.5 m — but the agency knowing something the
geometry does not is exactly the case where the feed should win.

Rebuilt wholesale on every run, for the same reason ingest is delete-and-reload
and the pattern tables are: this is a pure function of `stops` and `transfers`,
and a stale footpath is worse than no footpath at all.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from sqlalchemy import Connection, Engine, text

from a2transit.db.models import AgencySource
from a2transit.routing.constants import FOOTPATH_MAX_METRES, effective_transfer_seconds

logger = logging.getLogger(__name__)

#: Every stop pair inside the radius, plus every declared transfer whatever its
#: length, with the agency's declared time attached where there is one.
#:
#: The self-join is over `geography`, so ST_DWithin's third argument is metres
#: and the GiST index on stops.geog serves it directly. Ordered pairs: the
#: engines ask "where can I walk to from here", and a directed row is what that
#: lookup wants.
_PAIRS_SQL = """
    SELECT a.agency_source AS from_agency,
           a.stop_id       AS from_stop,
           b.agency_source AS to_agency,
           b.stop_id       AS to_stop,
           ST_Distance(a.geog, b.geog) AS metres,
           t.min_transfer_time AS declared
      FROM stops a
      JOIN stops b
        ON (a.agency_source, a.stop_id) <> (b.agency_source, b.stop_id)
       AND ST_DWithin(a.geog, b.geog, :radius)
      LEFT JOIN transfers t
        ON t.agency_source = a.agency_source
       AND t.from_stop_id  = a.stop_id
       AND t.to_stop_id    = b.stop_id
       AND t.transfer_type <> 3
     UNION
    SELECT t.agency_source, t.from_stop_id,
           t.agency_source, t.to_stop_id,
           ST_Distance(a.geog, b.geog),
           t.min_transfer_time
      FROM transfers t
      JOIN stops a ON a.agency_source = t.agency_source AND a.stop_id = t.from_stop_id
      JOIN stops b ON b.agency_source = t.agency_source AND b.stop_id = t.to_stop_id
     WHERE t.transfer_type <> 3
       AND t.from_stop_id <> t.to_stop_id
"""


@dataclass(frozen=True)
class FootpathBuildResult:
    total: int
    cross_agency: int
    declared: int
    #: Beyond FOOTPATH_MAX_METRES, so present only because an agency declares
    #: them. Zero in both feeds today.
    beyond_radius: int
    max_metres: float
    seconds: float

    @property
    def within_agency(self) -> int:
        return self.total - self.cross_agency


def _fetch_pairs(connection: Connection, radius: float) -> list[tuple]:
    return connection.execute(text(_PAIRS_SQL), {"radius": radius}).all()


def build_footpaths(
    engine: Engine, *, radius_metres: float = FOOTPATH_MAX_METRES
) -> FootpathBuildResult:
    """Rebuild the whole `footpaths` table. Idempotent."""
    started = time.perf_counter()

    with engine.begin() as connection:
        rows = _fetch_pairs(connection, radius_metres)

        # Walking time is computed here rather than in SQL so that the floor,
        # the speed and the detour allowance have exactly one definition. A
        # second copy of that arithmetic in a CASE expression is how the two
        # engines would quietly stop agreeing on what a transfer costs.
        payload = [
            {
                "from_agency": row.from_agency,
                "from_stop": row.from_stop,
                "to_agency": row.to_agency,
                "to_stop": row.to_stop,
                "metres": row.metres,
                "seconds": effective_transfer_seconds(row.declared, row.metres),
                "declared": row.declared,
            }
            for row in rows
        ]

        connection.execute(text("DELETE FROM footpaths"))
        if payload:
            connection.execute(
                text(
                    """
                    INSERT INTO footpaths (
                        from_agency_source, from_stop_id,
                        to_agency_source, to_stop_id,
                        metres, seconds, declared_seconds
                    ) VALUES (
                        :from_agency, :from_stop, :to_agency, :to_stop,
                        :metres, :seconds, :declared
                    )
                    """
                ),
                payload,
            )

    result = FootpathBuildResult(
        total=len(rows),
        cross_agency=sum(1 for row in rows if row.from_agency != row.to_agency),
        declared=sum(1 for row in rows if row.declared is not None),
        beyond_radius=sum(1 for row in rows if row.metres > radius_metres),
        max_metres=max((row.metres for row in rows), default=0.0),
        seconds=time.perf_counter() - started,
    )
    logger.info(
        "footpaths: %d links (%d cross-agency) within %.0f m in %.1fs",
        result.total,
        result.cross_agency,
        radius_metres,
        result.seconds,
    )
    return result


def cross_agency_pairs(engine: Engine, limit: int = 10) -> list[tuple[str, str, float]]:
    """The closest TheRide/MBus links, for the record in docs/feeds.md."""
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT a.stop_name AS from_name, b.stop_name AS to_name, f.metres
                  FROM footpaths f
                  JOIN stops a ON a.agency_source = f.from_agency_source
                              AND a.stop_id = f.from_stop_id
                  JOIN stops b ON b.agency_source = f.to_agency_source
                              AND b.stop_id = f.to_stop_id
                 WHERE f.from_agency_source = :theride
                   AND f.to_agency_source = :mbus
                 ORDER BY f.metres
                 LIMIT :limit
                """
            ),
            {
                "theride": AgencySource.THERIDE.value,
                "mbus": AgencySource.MBUS.value,
                "limit": limit,
            },
        ).all()
    return [(row.from_name, row.to_name, row.metres) for row in rows]
