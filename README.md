# a2transit

Door-to-door journey planning across Ann Arbor's two bus networks — **TheRide
(AAATA)**, the city system, and **U-M MBus**, the university campus network —
merged into a single routing graph, transfers between them included.

Neither agency's own trip planner will route you across the other's network,
even where their stops share a corner. [728 of their stop pairs sit within
400 m of each other, several within 2 m](docs/feeds.md#2-the-cross-agency-transfer-premise-is-real).

**Status:** M2 complete — reference routing engine planning real itineraries.

---

## What this is

| | |
|---|---|
| Routing | RAPTOR (round-based), validated against a time-dependent Dijkstra reference |
| Backend | Python 3.12 · FastAPI · SQLAlchemy |
| Data | PostgreSQL + PostGIS |
| Realtime | GTFS-Realtime pollers → Redis pub/sub → WebSocket |
| Frontend | React · Vite · MapLibre GL JS |
| Tiles | OpenFreeMap (no key, no account) |

Every dependency is free with no credit card on file. Deployment targets are
free tiers: Fly.io or Render (API), Neon or Supabase (Postgres), Upstash
(Redis), Vercel or Cloudflare Pages (frontend).

---

## Getting started

**Prerequisites:** Python 3.12+, Node 20+, Docker.

```bash
cp .env.example .env
docker compose up -d
```

Wait for both services to report healthy:

```bash
docker compose ps
```

**Backend** — from `backend/`:

```bash
python3.12 -m venv .venv && ./.venv/bin/pip install -e '.[dev]'
./.venv/bin/uvicorn a2transit.main:app --reload --port 8001
```

**Frontend** — from `frontend/`:

```bash
npm install && npm run dev
```

Then: API docs at <http://localhost:8001/docs>, app at <http://localhost:5174>.

> **Ports.** The API defaults to **8001** and Vite to **5174**, not the usual
> 8000/5173, because those are already in use by another project on the
> development machine. Change them in `docker-compose.yml`, `vite.config.ts`,
> and `.env` if you want the conventional ones.

### Health checks

| Endpoint | Meaning |
|---|---|
| `GET /health` | Process liveness. Always 200 if the app responds; touches no dependency. |
| `GET /ready` | Readiness. 200 when Postgres (with PostGIS) and Redis both answer, 503 otherwise, naming what failed. |

`/ready` returning 503 with `"database": "unavailable"` before you have run
`docker compose up` is correct behaviour, not a bug.

---

## Testing

```bash
cd backend
./.venv/bin/pytest              # 83 tests, offline
./.venv/bin/pytest -m network   # 8 more, against the live agency feeds
./.venv/bin/ruff check ..
```

Two markers keep the default run fast and self-contained:

| Marker | Behaviour |
|---|---|
| `network` | Deselected by default. Hits the live feeds — run before trusting anything about the data. |
| `db` | Runs by default, but **skips itself** when Postgres is unreachable, so the suite still passes on a plane. |

`db` tests use their own `a2transit_test` database, created on demand. They
never touch your development data — the loader's delete-and-reload would
otherwise wipe it on every run.

To re-verify every agency endpoint at once — worth doing whenever something
looks wrong with the data:

```bash
./backend/.venv/bin/python scripts/verify_feeds.py
```

---

## Loading the feeds

```bash
cd backend
./.venv/bin/python -m a2transit.ingest
```

Creates the schema on first run, then loads both agencies and prints row counts
per table. Unchanged feeds are skipped, so this is safe to run on a schedule —
TheRide's licence requires refreshing within three business days of a new
publication:

```bash
0 4 * * 1  cd /path/to/a2transit/backend && .venv/bin/python -m a2transit.ingest
```

`--agency theride|mbus` for one feed, `--force` to reload regardless,
`--from-file PATH` for a local ZIP.

Current scale (2026-08-23 feeds):

| | TheRide | MBus | total |
|---|---:|---:|---:|
| stops | 1,055 | 120 | 1,175 |
| routes | 30 | 16 | 46 |
| trips | 4,235 | 8,432 | 12,667 |
| stop_times | 106,066 | 108,658 | 214,724 |
| shape points | 73,507 | 19,102 | 92,609 |
| **all tables** | **185,005** | **136,695** | **321,700** |

Full load takes ~2.5 s.

## Planning a trip

```bash
cd backend
./.venv/bin/python -m a2transit.routing --search "YTC"
./.venv/bin/python -m a2transit.routing \
    --from theride:544 --to theride:1019 --depart 2026-09-10T09:00 -v
```

```
EB Washtenaw + Brookside -> E - First north of Frederick
  depart Thu 2026-09-10 09:00  arrive 09:37  (37 min, 1 transfer)
    09:00 EB Washtenaw + Brookside  --[theride 4 to Ypsilanti Transit Ctr]-->
    09:10 YTC - EndPt
    09:10 transfer to YTC - Stop 1 (20 min incl. wait)
    09:30 YTC - Stop 1  --[theride 47 to Hewitt & Ellsworth]-->
    09:37 E - First north of Frederick
```

A bare `stop_id` is rejected when both feeds use it — 90 do, as different
places — so stops are written `agency:stop_id`.

### What the engine does and does not do

M2 is the **correctness reference**, not the fast path. It uses a time-expanded
graph, where every node carries a time and every edge points forward, making the
graph a DAG whose topological order is time order. Earliest-arrival over it needs
no argument about FIFO or non-overtaking — which is the point, because M3's
RAPTOR gets checked against it.

| | |
|---|---|
| Timetable build | 560 ms per service date, amortised across queries |
| Query p50 / p95 | **146 ms / 198 ms** over 200 random pairs |
| Criterion | Earliest arrival only. Fewest-transfers arrives with RAPTOR in M3. |

Known limitations, all deliberate:

- **No cross-agency itineraries yet.** The feeds share no `stop_id`, and the only
  inter-stop links in the data are TheRide's 17 declared transfers, all inside
  Ypsilanti Transit Center. PostGIS footpaths in M4 are what join the two
  networks; until then a TheRide-to-MBus query correctly returns nothing.
- **Tight timed transfers at pulse points are rejected.** Every TheRide transfer
  declares `min_transfer_time = 10 s` across bays up to 71 m apart — 25 km/h on
  foot — so a 60 s floor is applied instead. TheRide does hold connecting buses
  at Blake Transit Center, but GTFS has no way for them to say so in the data
  they publish, so nothing distinguishes a held connection from a coincidental
  one.
- **Boardings are bounded by a six-hour horizon.** A ride boarded inside it may
  finish outside it; cutting a trip off mid-ride would report "unreachable" for
  a bus the rider is already on.

### Service dates are not calendar days

Two things the engine has to get right, both of which fail quietly rather than
loudly:

**GTFS times pass 24:00:00.** MBus reaches `27:15:00`. Times are stored as
integer seconds from service midnight and normalised against a {D−1, D, D+1}
window, so a query at 00:30 still sees the buses that belong to yesterday's
service date.

**`calendar_dates` is load-bearing, not an edge case.** MBus overlays several
`service_id`s onto each weekday and removes most again by exception. Reading
`calendar.txt` alone gives 3,620 trips on an ordinary Thursday instead of 1,668,
and 3,490 on Labor Day instead of 366.

## Data sources

Full detail, provenance, and licence terms: **[docs/feeds.md](docs/feeds.md)**.

| Agency | Static GTFS | GTFS-Realtime |
|---|---|---|
| TheRide (AAATA) | `theride.org/sites/default/files/google/google_transit.zip` | `rt.theride.org/gtfsrt/{vehicles,trips,alerts}` |
| U-M MBus | `webapps.fo.umich.edu/transit_uploads/google_transit.zip` | `mbus.ltp.umich.edu/gtfsrt/{vehicles,trips,alerts}` |

Two caveats worth knowing before you trust these:

1. **The URLs most often cited online are dead.** Both agencies moved their
   static feed; the widely-linked `theride.org/google/google_transit.zip` now
   404s. `scripts/verify_feeds.py` exists so this fails loudly, early.
2. **TheRide's realtime endpoints are undocumented.** They serve unauthenticated,
   spec-compliant GTFS-Realtime v2.0 protobuf, but they appear in no feed
   registry and on no developer page — [see how they were traced](docs/feeds.md#provenance-of-the-realtime-urls--read-this-before-depending-on-them).
   Treat realtime as best-effort: routing must fall back to schedule-only when
   it is unavailable.

### Attribution

TheRide's data licence requires this notice be displayed prominently wherever
their data appears, and that the GTFS data be refreshed within three business
days of a new publication:

> Transit scheduling, geographic, and real-time data provided by permission of AAATA/TheRide.

---

## Architecture

```
GTFS static (TheRide + MBus)  ──▶  ingest job  ──▶  Postgres + PostGIS
                                                        │
                                          preprocessing: RAPTOR route-patterns,
                                          stop→routes index, footpaths (PostGIS)
                                                        │
GTFS-Realtime (both agencies)  ──▶  poller  ──▶  Redis  ──▶  routing engine (RAPTOR)
                                             │                   │
                                             │            FastAPI /plan endpoint
                                             │                   │
                                     WebSocket push  ──────▶  React + MapLibre frontend
```

### The one design constraint you cannot ignore

**Stop, trip, and service IDs collide across the two agencies, and the
collisions are meaningless.** 90 `stop_id` values, 800 `trip_id` values, and all
three of TheRide's `service_id` values appear in both feeds. Of the 90 colliding
stop IDs, exactly one pair is co-located — TheRide's stop `161` is "Tyler +
Zephyr", MBus's is "TEST STOP 1", 14.9 km away.

The `service_id` collision is the dangerous one, because it fails quietly:
TheRide's `3` means Mon–Fri, MBus's `3` means Monday only.

So every key is composite — `(agency_source, stop_id)`, `(agency_source,
trip_id)` — including when resolving a `trip_id` off a realtime feed. Getting
this wrong does not throw; it silently routes a rider onto the wrong agency's
bus.

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
- [ ] **M3 — Routing v2 (RAPTOR).** Round-based, earliest-arrival and
      fewest-transfers. *Matches v1 on every M2 test; median query < 50 ms.*
- [ ] **M4 — Walking / footpaths.** PostGIS `ST_DWithin` stop→stop links
      (~400 m, cross-agency included); connect arbitrary lat/lon to nearby
      stops. *Door-to-door plan between two street addresses.*
- [ ] **M5 — HTTP API.** `/plan`, `/stops/search`, `/stops/{id}/departures`.
      *OpenAPI at `/docs`; every endpoint tested.*
- [ ] **M6 — Frontend.** Geocoding, stop autocomplete, route drawn on the map,
      itinerary panel. *Mobile-responsive, deployed-quality.*
- [ ] **M7 — Realtime.** Poll GTFS-RT into Redis; delay-adjusted arrival times;
      WebSocket vehicle markers and alerts. *Simulated delay moves an
      itinerary's arrival time and a marker live.*
- [ ] **M8 — Polish + deploy.** Shareable trip URLs, live departures board,
      error and empty states, all four tiers on free plans. *Public URL.*
- [ ] **M9 — Measure.** Feed scale, query latency p50/p95, correctness
      spot-checks, realtime end-to-end lag — recorded here.

## Repository layout

```
backend/
  a2transit/
    api/         FastAPI routers
    db/          SQLAlchemy engine, session, models
    ingest/      GTFS static parsing and loading      (M1)
    routing/     Dijkstra reference + RAPTOR          (M2, M3)
    realtime/    GTFS-RT pollers, Redis, WebSocket    (M7)
  tests/
frontend/        Vite + React + MapLibre
docker/initdb/   Extensions installed on first DB boot
docs/feeds.md    Feed endpoints, provenance, licences
scripts/         verify_feeds.py and operational scripts
```
