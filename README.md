# a2transit

Door-to-door journey planning across Ann Arbor's two bus networks — **TheRide
(AAATA)**, the city system, and **U-M MBus**, the university campus network —
merged into a single routing graph, transfers between them included.

Neither agency's own trip planner will route you across the other's network,
even where their stops share a corner. [732 of their stop pairs sit within
400 m of each other, dozens within 5 m](docs/feeds.md#2-the-cross-agency-transfer-premise-is-real).

**Status:** M0 complete — feeds verified, scaffold in place.

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
./.venv/bin/pytest              # offline suite
./.venv/bin/pytest -m network   # also hit the live agency feeds
./.venv/bin/ruff check ..
```

Tests that reach out to the internet carry the `network` marker and are
deselected by default, so the suite runs on a plane.

To re-verify every agency endpoint at once — worth doing whenever something
looks wrong with the data:

```bash
./backend/.venv/bin/python scripts/verify_feeds.py
```

---

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

**Stop and trip IDs collide across the two agencies, and the collisions are
meaningless.** 90 `stop_id` values and 800 `trip_id` values appear in both
feeds; of the 90 colliding stop IDs, exactly one pair is co-located. TheRide's
stop `161` is "Tyler + Zephyr"; MBus's stop `161` is "TEST STOP 1", 14.9 km
away.

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
- [ ] **M1 — GTFS ingest.** Load both agencies into Postgres with a sane schema
      and indexes; idempotent weekly `refresh`. *Row counts per table; spot-check
      TheRide Route 4 by query.*
- [ ] **M2 — Routing v1 (correctness).** Time-dependent Dijkstra over a
      time-expanded graph. *Hand-verified itineraries for 5+ real stop pairs.*
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
