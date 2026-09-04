# Deployment

## Deploying

**Live at [a2transit.vercel.app](https://a2transit.vercel.app)**, API at
[a2transit-api.onrender.com](https://a2transit-api.onrender.com/docs), across
four free tiers with no card on any of them:

| Tier | Service | What it costs |
|---|---|---|
| Frontend | Vercel | free, CDN, always on |
| API + poller | Render | free, sleeps after 15 min idle |
| Postgres + PostGIS | Neon | free, no expiry |
| Redis | Upstash | free |

Three things that only showed up once it was actually deployed, all fixed here:

- **`$PORT`.** Render assigns the port. Hardcoding 8000 works — the platform
  finds the open port eventually — but costs a network reconfigure and restart
  on every deploy. The `CMD` is `sh -c` for variable expansion and `exec` inside
  it so uvicorn stays PID 1; without the `exec`, `sh` swallows SIGTERM and the
  container is SIGKILLed after the grace period instead of shutting the inline
  poller down cleanly. Verified: stops in 1 s, not 10.
- **Memory.** 512 MB, and the cache defaults to four service dates *per engine* —
  and `?engine=dijkstra` is public, so a visitor can ask for the expensive one.
  `TIMETABLE_CACHE_SIZE=2` on Render.
- **Blueprint env vars.** The three `sync: false` values must be entered when the
  Blueprint is applied. Miss them and the app falls back to the localhost
  defaults in `config.py`, `/ready` 503s against a database that is not there,
  and the deploy fails a health check for fifteen minutes before giving up.

Everything needed is committed: `backend/Dockerfile`, `fly.toml`, `render.yaml`
and `frontend/vercel.json`. What is *not* committed is any account — creating
those and pasting in the credentials is the one part that has to be done by
hand.

| Tier | Service | Free plan |
|---|---|---|
| API + poller | Fly.io or Render | yes, scales to zero |
| Postgres + PostGIS | Neon or Supabase | yes |
| Redis | Upstash | yes |
| Frontend | Vercel or Cloudflare Pages | yes |

```bash
# 1. Provision Postgres (with PostGIS) and Redis, then load the feeds into it.
#    The ingest runs from your machine against the remote database — it is a
#    one-off, and there is no reason to ship 345,160 rows through the API.
DATABASE_URL='postgresql+psycopg://...' backend/.venv/bin/python -m a2transit.ingest

# 2. API. Render reads render.yaml as a Blueprint; Fly reads fly.toml.
fly launch --no-deploy
fly secrets set DATABASE_URL=... REDIS_URL=... CORS_ORIGINS=https://your-frontend
fly deploy

# 3. Frontend
VITE_API_BASE_URL=https://your-api npm --prefix frontend run build
```

The image is built and run against a live database before every deploy-affecting
change: it plans the cross-agency Blake→Central Campus trip from inside the
container, with the production environment set.

## Two hosts, one difference that matters

`fly.toml` runs the realtime poller as its own process, which is the right shape
— six feeds fetched once, by one consumer, whatever the API is doing.

Render's free plan has no worker tier, so `render.yaml` sets
`REALTIME_INLINE_POLL=true` and the API polls for itself. That is safe here only
because the image runs a single uvicorn worker, for reasons that predate it: the
timetable cache is per-process, so every worker is another copy of it. One
poller per process is still one poller. Enabling it *alongside* a real worker,
or under multiple uvicorn workers, would multiply the request rate at two
unauthenticated endpoints neither agency has promised us anything about — which
is why it is an explicit setting and not a guess about the environment.

It also turns out to suit a tier that sleeps. A separate poller keeping a
sleeping API's data warm around the clock is work nobody benefits from; the
snapshot has expired by the time a visitor arrives. Polling in-process fetches
the feeds exactly when somebody is looking.

## The free tier sleeps, and the UI says so

A Render free service spins down after fifteen minutes idle, and the request
that wakes it can take most of a minute. The frontend treats a first failure as
*probably asleep* rather than *down*: it retries with a rising delay for 75
seconds, and the header reads `◌ waking the server… 12s` instead of a red
`API unreachable`. It flips to live the moment the container answers, with no
reload.

Which matters more than it sounds, because the first person to open a link
somebody sent them is exactly the person who must not see a planner that looks
broken.

Three things about the deployment that are load-bearing rather than incidental:

- **`/ready` treats Redis as optional.** Postgres missing is a 503 — there is
  nothing to plan on. Redis missing is a 200 with `"status": "degraded"`,
  because realtime is an enhancement by construction and failing readiness would
  pull a working planner out of the load balancer over the loss of a feature it
  is designed to survive.
- **One uvicorn worker per instance.** The timetable cache is per-process, so
  four workers is four copies of the same tables. Measured on the 2026-08-23
  feeds: 68 MB idle, +54 MB for the first RAPTOR timetable, +31 MB for each
  further service date — the trip stop sequences are shared and only the
  per-date instances are new. Scale with instances, and set
  `TIMETABLE_CACHE_SIZE` to suit the box: the default of 4 dates per engine
  does not fit a 512 MB free tier once the Dijkstra timetables and live
  overlays are counted, and `?engine=dijkstra` is public, so a visitor can ask
  for the expensive one.
- **The SPA needs a catch-all rewrite.** A shared trip link carries its state in
  the query string, and without the rewrite it 404s on reload — which is the one
  URL people actually paste.

## Realtime

```bash
cd backend
./.venv/bin/python -m a2transit.realtime              # poll both agencies
./.venv/bin/python -m a2transit.realtime --once       # one cycle
./.venv/bin/python -m a2transit.realtime --simulate-delay theride:3572020:900
```

Six feeds — vehicles, trips and alerts for each agency — every 20 seconds into
Redis. The poller is one process regardless of how many API workers run: the
agencies see one client, and an API restart does not interrupt the feed.

**Realtime is an overlay, not a mode.** Predictions are folded into a copy of
the cached schedule timetable, and RAPTOR is handed something it cannot tell
from the schedule — so the Pareto set, the horizon, the transfer floor and every
differential test keep working unchanged. The consequence worth having: a
delayed bus is not merely reported as late, it *loses*. Delay route 4's 06:02 by
fifteen minutes and the planner puts the rider on the 06:10 instead.

Everything written to Redis expires. Stale realtime is worse than none — a
five-minute-old "on time" will send someone running for a bus that has gone — so
if the poller dies the keys drain and planning falls back to the schedule
inside two minutes, with no health check to wire up and no code path that only
runs during an outage.

Neither agency publishes `delay`; both publish absolute predicted times. A delay
therefore does not exist until something compares a prediction to the schedule,
which is why `realtime/delays.py` is the only module that holds both.
