# Measurements

Everything below comes from one command, so the numbers can be re-checked after
a feed refresh rather than trusted because they are written down:

```bash
./backend/.venv/bin/python scripts/measure.py --cases 200 --realtime
```

Measured 2026-09-03 against the 2026-08-23 feeds, on an M-series laptop.

**Scale.** 345,160 rows: 214,724 stop_times, 92,609 shape points, 12,667 trips,
117 route patterns over 2,498 pattern-stops, and 8,308 footpaths of which 1,456
cross between the agencies.

**Build cost.** RAPTOR timetable 315 ms; Dijkstra timetable 518 ms; the
time-expanded graph 185 ms for 113,213 nodes and 393,618 edges.

**Query latency**, 200 seeded cases across a weekday, Labor Day and a Saturday:

| | p50 | p95 |
|---|---:|---:|
| RAPTOR | **3.1 ms** | **7.5 ms** |
| Dijkstra reference | 59.6 ms | 238.6 ms |

19× at p50, 32× at p95, against an acceptance target of 50 ms. Door-to-door
between two addresses is 5.6 ms p50 for the query plus 3.3 ms to attach the two
places, which is one PostGIS round trip.

**Correctness.** 0 arrival mismatches over 500 differential cases. 49 of 200
cases pick different trips at an identical arrival time, which is a tie broken
differently and not a disagreement. The four spot-checks the script runs — a
direct trip, a cross-agency trip, a three-option trade-off, and a post-midnight
arrival — all land on their hand-verified answers.

**Realtime.** Feeds are 14–32 s old when fetched, which is the agencies'
publication lag and not something a consumer can improve. Applying 239 live
predictions to the timetable takes 12 ms and moves 226 runs across 48 patterns;
worst delay observed in that sample was 23 minutes. Stored snapshots were
33–45 s old against a 120 s expiry, so the schedule fallback had ~75 s of
headroom.

The honest summary of where the time goes: a query is ~3 ms, and everything
around it — building a timetable, attaching a place, folding in realtime — is
between 3 ms and 500 ms. Which is why the timetables are cached per service date
and the realtime overlay is cached per poll.

## Numbers

| | |
|---:|---|
| **3.1 ms** | median plan, p95 7.5 ms — against a 50 ms target |
| **19×** | faster than the Dijkstra reference it is checked against |
| **0** | arrival mismatches across 500 seeded differential cases |
| **8,308** | generated footpaths, 1,456 of them crossing between agencies |
| **345,160** | rows loaded from both feeds, in ~2.5 s |
| **376** | tests — 350 backend, 26 frontend |
| **$0** | every tier runs on a free plan, no credit card |

All of it reproducible: [`scripts/measure.py`](scripts/measure.py) prints every
figure above in one run. Details under [Measurements](#measurements).

Those are engine numbers, measured on a laptop. **The public demo is slower and
honestly so:** Render's free plan throttles CPU hard, and the same query that
takes 3.1 ms locally takes 80–170 ms there. Nothing in the code differs — it is
the same container — and it is worth knowing which number is which before
quoting either.

---

## Milestones

- [x] **M0 — Verify feeds + scaffold.** Confirm live GTFS static and GTFS-RT
      endpoints for both agencies; document formats and licence terms. FastAPI
      app, docker-compose (PostGIS + Redis), Vite/React skeleton.
      *Done: `docker compose up` gives a working DB; `GET /health` returns 200.*
- [x] **M1 — GTFS ingest.** Load both agencies into Postgres with a sane schema
      and indexes; idempotent weekly `refresh`.
      *Done: 321,700 rows across both feeds in ~2.5 s; reload is byte-identical;
      Route 4 verified against the published schedule.*
- [x] **M2 — Routing v1 (correctness).** Earliest-arrival Dijkstra over a
      time-expanded graph.
      *Done: 6 hand-verified pairs incl. a cross-midnight arrival and a holiday
      service change; p50 146 ms / p95 198 ms over 200 random queries.*
- [x] **M3 — Routing v2 (RAPTOR).** Round-based, earliest-arrival and
      fewest-transfers.
      *Done: 0 mismatches over 500 seeded differential cases; p50 1.4 ms on
      routable queries against Dijkstra's 114 ms.*
- [x] **M4 — Walking / footpaths.** PostGIS `ST_DWithin` stop→stop links
      (400 m, cross-agency included); arbitrary lat/lon joined to nearby stops.
      *Done: 8,308 footpaths, 1,456 across agencies; door-to-door between two
      street addresses; 0 mismatches over 500 differential cases after the
      differential caught two engine bugs.*
- [x] **M5 — HTTP API.** `/plan`, `/stops/search`, `/stops/{id}/departures`,
      `/geocode`. *Done: OpenAPI at `/docs`; the Pareto set with each ride leg's
      real shape; timetables cached per service date.*
- [x] **M6 — Frontend.** Geocoding, stop autocomplete, route drawn on the map,
      itinerary panel. *Done: one field takes a stop or an address; mobile
      layout; walk legs dashed, rides drawn along the published shape.*
- [x] **M7 — Realtime.** Poll GTFS-RT into Redis; delay-adjusted arrival times;
      WebSocket vehicle markers and alerts. *Done: ~250 predictions folded into
      the timetable in 50 ms; a 15-minute delay on route 4's 06:02 does not just
      move the arrival, it moves the rider to the 06:10.*
- [x] **M8 — Polish + deploy.** Shareable trip URLs, live departures board,
      filtered service alerts, keyboard-navigable search, error and empty
      states, and a container plus manifests for all four tiers on free plans.
      *Deploy-ready; the public URL needs accounts I do not have — see below.*
- [x] **M9 — Measure.** Feed scale, query latency p50/p95, correctness
      spot-checks, realtime end-to-end lag. *Done: `scripts/measure.py` produces
      every figure below in one run, so they can be re-checked rather than
      remembered.*
