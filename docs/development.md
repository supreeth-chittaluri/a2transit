# Running it locally

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

Type a stop name or a street address into either field, or click the map. The
planner returns every non-dominated option — fewest changes first, fastest
last — and draws the selected one along the route's published shape.

> **Ports.** The API defaults to **8001** and Vite to **5174**, not the usual
> 8000/5173, because those are already in use by another project on the
> development machine. Change them in `docker-compose.yml`, `vite.config.ts`,
> and `.env` if you want the conventional ones.

## Testing

```bash
cd backend
./.venv/bin/pytest              # 344 tests
./.venv/bin/pytest -m slow      # the 500-case differential
./.venv/bin/pytest -m network   # against the live agency feeds
./.venv/bin/ruff check ..

cd ../frontend
npm test                        # 26 tests, no browser needed
```

The frontend tests cover the browser-shaped logic that has no oracle: trip
links round-tripping through the address bar, and the local-time formatting
that `toISOString()` would silently shift by four hours. The routing itself is
checked against two independent engines in Python, which is a much stronger
thing to have than a React component snapshot.

Two markers keep the default run fast and self-contained:

| Marker | Behaviour |
|---|---|
| `network` | Deselected by default. Hits the live feeds and the geocoder — run before trusting anything about the data. |
| `slow` | Deselected by default. The full 500-case differential between the two engines. |
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

Creates the schema on first run, loads both agencies, then rebuilds everything
derived from them — RAPTOR patterns and the PostGIS footpath table. Unchanged
feeds are skipped, so this is safe to run on a schedule —
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

Full load takes ~2.5 s. Preprocessing then derives 117 route patterns and
**8,308 footpaths** (1,456 of them between the two agencies) in under a second.

## Planning a trip

```bash
cd backend
./.venv/bin/python -m a2transit.routing --search "YTC"
./.venv/bin/python -m a2transit.routing \
    --from theride:1605 --to mbus:207 --depart 2026-09-10T09:00 -v
./.venv/bin/python -m a2transit.routing \
    --from-place "Kerrytown, Ann Arbor" --to-place "Michigan Stadium" \
    --depart 2026-09-10T09:00
```

```
Temp BTC endpt -> Central Campus Transit Center: Ruthven Museum
  depart Thu 2026-09-10 09:00  arrive 09:12  (12 min, 1 transfer)
    09:00 transfer to WB Washington + Fifth (1) (2 min incl. wait)
    09:02 WB Washington + Fifth (1)  --[theride 4]-->
    09:06 S - Huron  east of Ingalls
    09:06 transfer to Rackham Bldg (3 min incl. wait)
    09:08 Rackham Bldg  --[mbus CS]-->
    09:10 Central Campus Transit Center: Chemistry
    09:10 transfer to Central Campus Transit Center: Ruthven Museum (2 min incl. wait)
```

That journey uses both agencies and could not be answered before M4. A bare
`stop_id` is rejected when both feeds use it — 90 do, as different places — so
stops are written `agency:stop_id`. An endpoint may equally be an address
(`--from-place`, geocoded) or a coordinate pair (`--from-latlon`).

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
