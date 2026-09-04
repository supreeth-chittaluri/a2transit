# HTTP API

## The API

| Endpoint | What it does |
|---|---|
| `GET /plan?from=&to=&depart=` | Every non-dominated itinerary. Each end is `agency:stop_id` or `lat,lon`, and the two mix freely. Ride legs carry the route's real geometry, clipped out of the GTFS shape. |
| `GET /stops/search?q=` | Trigram autocomplete over both feeds' stops. |
| `GET /stops/{agency}/{id}/departures` | The next departures, read off the router's own timetable so the board and the planner agree about holidays. |
| `GET /geocode?q=` | Address to coordinates. Proxied server-side because Nominatim's policy asks for a real User-Agent and one request a second, which a browser tab cannot promise. |
| `GET /realtime/status` | Whether live data is flowing, and how old each feed is. |
| `GET /realtime/vehicles` · `/realtime/alerts` | Snapshots, for callers that would rather not hold a socket open. |
| `WS /ws/vehicles` | Live positions. Opens with the current snapshot, then forwards each poll. Every frame is a whole snapshot, never a diff, so a dropped frame costs nothing and reconnection needs no reconciliation. |

```bash
curl 'localhost:8001/plan?from=theride:1605&to=mbus:207&depart=2026-09-10T09:00'
```

## Health checks

| Endpoint | Meaning |
|---|---|
| `GET /health` | Process liveness. Always 200 if the app responds; touches no dependency. |
| `GET /ready` | Readiness. 200 when Postgres (with PostGIS) and Redis both answer, 503 otherwise, naming what failed. |

`/ready` returning 503 with `"database": "unavailable"` before you have run
`docker compose up` is correct behaviour, not a bug.

---
