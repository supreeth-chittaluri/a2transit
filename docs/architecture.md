# Architecture

```
GTFS static (TheRide + MBus)  ──▶  ingest job  ──▶  Postgres + PostGIS
                                                        │
                                          preprocessing: RAPTOR route-patterns,
                                          stop→routes index, footpaths (PostGIS
                                          ST_DWithin, 400 m, cross-agency)
                                                        │
GTFS-Realtime (both agencies)  ──▶  poller  ──▶  Redis  ──▶  timetable overlay
                                             │                   │
                                             │            FastAPI /plan endpoint
                                             │                   │
                                     WebSocket push  ──────▶  React + MapLibre frontend
```

## The one design constraint you cannot ignore

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

## Why it is harder than it sounds

| | |
|---|---|
| **The IDs collide, and silently** | 90 `stop_id`s, 800 `trip_id`s and all three of TheRide's `service_id`s appear in both feeds as different things. TheRide's service `3` means Mon–Fri; MBus's means Monday only. Joining them yields a *plausible* schedule that is wrong. Every key in the schema is composite. |
| **Time is not a clock** | GTFS times pass midnight — MBus reaches `27:15:00`. Stored as integer seconds from service midnight over a {D−1, D, D+1} window, so a query at 00:30 still sees yesterday's buses. |
| **The calendar is load-bearing** | Reading `calendar.txt` alone gives 3,620 MBus trips on an ordinary Thursday instead of 1,668, and 3,490 on Labor Day instead of 366. |
| **Fast and correct are different programs** | RAPTOR is the engine; a time-expanded Dijkstra is kept as an oracle and 500 seeded cases are run through both. It has caught three real bugs that tests written against the fast path would have agreed with. |
| **The feeds move** | Both agencies' published GTFS URLs were dead when this started. TheRide's realtime endpoints are in no registry at all and were traced through their Clever Devices backend. |
