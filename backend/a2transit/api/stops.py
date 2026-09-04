"""Stop lookup and departure boards.

`/stops/search` is the autocomplete behind the frontend's origin and destination
fields. It leans on the `gin_trgm_ops` index created in M1: substring matching
over 1,175 stops would be fine unindexed, but trigram similarity is what makes
"kerytown" find "SB Fifth Av + Kerrytown", and LIKE will never do that.

It matters more here than it would elsewhere, because the names riders know are
often not the names in the feed. TheRide's central hub is Blake Transit Center;
the feed calls it "Temp BTC endpt", because it is being rebuilt. Nobody types
that.

`/stops/{agency}/{stop_id}/departures` reads the timetable rather than the
database, because "what leaves here next" has to respect the same service
calendar and the same {D-1, D, D+1} window the router does. Answering it with a
query over stop_times would give a board that disagrees with the planner about
which buses exist — on Labor Day, by a factor of ten.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import text

from a2transit.api.schemas import (
    DepartureModel,
    DeparturesResponse,
    StopRef,
    StopSearchResponse,
    StopSearchResult,
)
from a2transit.api.state import timetable_cache
from a2transit.db.models import AgencySource
from a2transit.db.session import get_engine

router = APIRouter(prefix="/stops", tags=["stops"])

DEFAULT_SEARCH_LIMIT = 12
DEFAULT_DEPARTURE_LIMIT = 20

#: Trigram similarity first, then a prefix bonus, then name.
#:
#: The LIKE patterns are bound rather than concatenated in SQL. A literal `%`
#: in a text() statement has to be doubled for the driver's paramstyle and is
#: then passed through *unchanged* — the query matches nothing and raises
#: nothing, which is the worst of both.
#:
#: The ILIKE term is a filter, not a ranking. Without it a fuzzy match on an
#: unrelated name can outrank a stop whose name literally contains the query;
#: without the similarity term, a typo returns nothing at all.
_SEARCH_SQL = """
    SELECT s.agency_source, s.stop_id, s.stop_name, s.stop_lat, s.stop_lon,
           COALESCE(
               array_agg(DISTINCT r.route_short_name)
                   FILTER (WHERE r.route_short_name IS NOT NULL),
               '{}'
           ) AS routes
      FROM stops s
      LEFT JOIN stop_times st
             ON st.agency_source = s.agency_source AND st.stop_id = s.stop_id
      LEFT JOIN trips t
             ON t.agency_source = st.agency_source AND t.trip_id = st.trip_id
      LEFT JOIN routes r
             ON r.agency_source = t.agency_source AND r.route_id = t.route_id
     WHERE s.stop_name ILIKE :contains
        OR similarity(s.stop_name, :query) > 0.2
     GROUP BY s.agency_source, s.stop_id, s.stop_name, s.stop_lat, s.stop_lon
     ORDER BY (s.stop_name ILIKE :starts_with) DESC,
              similarity(s.stop_name, :query) DESC,
              s.stop_name
     LIMIT :limit
"""


@router.get("/search", response_model=StopSearchResponse, summary="Find a stop by name")
def search(
    query: str = Query(..., alias="q", min_length=2, examples=["ytc"]),
    limit: int = Query(DEFAULT_SEARCH_LIMIT, ge=1, le=50),
) -> StopSearchResponse:
    with get_engine().connect() as connection:
        rows = connection.execute(
            text(_SEARCH_SQL),
            {
                "query": query,
                "contains": f"%{query}%",
                "starts_with": f"{query}%",
                "limit": limit,
            },
        ).all()

    return StopSearchResponse(
        query=query,
        results=[
            StopSearchResult(
                id=f"{row.agency_source}:{row.stop_id}",
                name=row.stop_name,
                agency=row.agency_source,
                lat=row.stop_lat,
                lon=row.stop_lon,
                routes=sorted(row.routes),
            )
            for row in rows
        ],
    )


@router.get(
    "/{agency}/{stop_id}/departures",
    response_model=DeparturesResponse,
    summary="What leaves this stop next",
)
def departures(
    agency: AgencySource,
    stop_id: str,
    at: dt.datetime | None = Query(None, description="ISO 8601 local time; defaults to now."),
    limit: int = Query(DEFAULT_DEPARTURE_LIMIT, ge=1, le=100),
) -> DeparturesResponse:
    moment = at or dt.datetime.now().replace(microsecond=0)
    timetable = timetable_cache().raptor(moment.date())

    key = (agency, stop_id)
    stop = timetable.stops.get(key)
    if stop is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"no stop {agency.value}:{stop_id}"
        )

    midnight = dt.datetime.combine(timetable.base_date, dt.time())
    now_seconds = int((moment - midnight).total_seconds())

    found: list[tuple[int, DepartureModel]] = []
    for pattern_index, position in timetable.stop_index.get(key, ()):
        pattern = timetable.patterns[pattern_index]
        if not pattern.can_board[position] or position == len(pattern.stops) - 1:
            continue
        route = timetable.routes.get((pattern.agency, pattern.route_id))
        for run in pattern.runs:
            seconds = run.departures[position]
            if seconds < now_seconds:
                continue
            found.append(
                (
                    seconds,
                    DepartureModel(
                        agency=pattern.agency.value,
                        route_id=pattern.route_id,
                        route_label=route.label if route else pattern.route_id,
                        route_color=f"#{route.color}" if route and route.color else None,
                        trip_id=run.trip_id,
                        headsign=timetable.stops[pattern.stops[-1]].name,
                        departure=midnight + dt.timedelta(seconds=seconds),
                        in_seconds=seconds - now_seconds,
                    ),
                )
            )

    found.sort(key=lambda item: item[0])
    return DeparturesResponse(
        stop=StopRef.of(stop),
        at=moment,
        departures=[model for _, model in found[:limit]],
    )
