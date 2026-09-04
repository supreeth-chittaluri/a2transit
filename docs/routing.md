# Routing

## Two engines

**RAPTOR** (M3) is the one you use. **Dijkstra over a time-expanded graph** (M2)
is kept as the correctness reference: every node carries a time and every edge
points forward, making the graph a DAG whose topological order is time order, so
earliest-arrival needs no argument about FIFO or non-overtaking. That is exactly
what makes it a trustworthy oracle rather than a second opinion.

| | RAPTOR | Dijkstra |
|---|---:|---:|
| p50 | **3.1 ms** | 60 ms |
| p95 | **7.5 ms** | 239 ms |
| Timetable build | 315 ms | 518 ms |
| Criteria | earliest arrival **and** fewest transfers | earliest arrival |

RAPTOR is roughly **19x faster** at p50 and 32x at p95, against an acceptance
target of 50 ms. Both engines got slower when M4 added 8,308 footpaths, which is
the cost of the two networks being one; RAPTOR went from 1.4 ms to 3.1 ms. Full
figures, and the command that produces them, under [Measurements](#measurements).

The network is small — 42 GTFS routes become 117 patterns over 2,498
pattern-stops — so a round is ~2,500 stop visits, where the Dijkstra searches a
113,000-node, 394,000-edge graph.

Footpaths could have made that graph five times larger. Every walk landing at
its own exact time would add ~467,000 nodes; instead a walk edge points at the
first platform node at or after it lands, because landing is only ever a
prelude to boarding and every departure is already a platform time. Node count
is unchanged, and walks originate only at vehicle arrivals and the origin —
which is what makes a second walk in a row structurally impossible rather than
merely discouraged.

Pick an engine with `--algorithm raptor|dijkstra`, or run both with `--compare`.

## Walking, and why it took a milestone

Footpaths are what make "TheRide + MBus as one network" true rather than
aspirational. Before them the two agencies touched nowhere: they publish 15
usable transfers between them, all TheRide's, all inside Ypsilanti Transit
Center, so a TheRide-to-MBus query correctly returned nothing.

One `ST_DWithin` self-join over the GiST index on `stops.geog` produces 8,308
directed links at 400 m, 1,456 of them crossing between the feeds. Three things
had to change together, because changing any one alone makes the two engines
disagree:

1. **Both engines read the same table.** That retired a transitive closure both
   of them ran to work around TheRide declaring `103→108` and `108→101` and no
   `103→101`. The retired algorithm now lives in the test that justified its
   deletion, applied to the live feed: every edge it invents exists directly in
   the 400 m set.
2. **Walking into a stop is arriving there.** Both engines targeted vehicle
   arrivals only, so a rider dropped 100 m from their destination was told the
   trip was impossible.
3. **A place is a stop no vehicle serves.** An arbitrary lat/lon becomes a
   synthetic stop joined to the network by footpaths, so neither engine needs
   any notion of a place — and door-to-door queries are differentially tested
   by exactly the machinery that tests stop-to-stop ones.

The differential earned its keep here. It caught RAPTOR pruning a vehicle
arrival against an earlier arrival *on foot* at the same stop, which is
tempting and wrong: only a vehicle arrival licenses walking onward, so the
better-looking label was the unusable one. Four of 500 cases, all on one
Saturday, all found by comparing against an engine that cannot make that
mistake because it does not have the concept.

## How the second criterion is verified

Fewest-transfers has no oracle in M2, which answers earliest arrival only, so it
rests on three independent legs rather than on differential testing alone:

1. **A bounded-transfers oracle** (`routing/bounded.py`) extends the M2 search
   with a vehicle budget — labels become `(node, boardings)` over the same DAG.
   Slow, and reaching the answer by constrained shortest path rather than by
   rounds, so agreement is evidence. It checks *every* entry of the Pareto set,
   not just the fastest.
2. **Hand-verified pairs where the criteria disagree**, read out of `stop_times`
   first. MBus 218 to 247 at 08:45 offers 1 transfer arriving 09:43:55 or 2
   transfers arriving 09:38:59.
3. **Invariants on every query** — arrivals strictly decrease as transfers
   increase, leg count matches the declared transfer count, no entry is
   dominated, legs chain, every transfer clears the floor.

Differential testing against M2 is seeded (`compare.SEED = 20260910`), so case N
is the same case on every run and a failure prints the command that replays it.
500 cases across a weekday, Labor Day and a Saturday: **0 arrival mismatches**.
54 cases pick different trips with identical arrival times, which is a tie broken
differently and not a disagreement.

Known limitations, all deliberate:

- **Walking is a straight line with a detour allowance.** Distances come from
  PostGIS `ST_Distance` over `geography` and are multiplied by 1.3 before
  becoming seconds. A real network router (OSRM) would do better, and is
  deliberately kept off the critical path: it is a rate-limited demo server
  with no SLA, and a journey planner must not stop working because someone
  else's free service is busy.
- **One footpath per leg, never two in a row.** A rider may walk at most 400 m
  between vehicles, and 800 m to reach the network or leave it. Chaining walks
  would let the planner route someone across town on foot in a round.
- **Tight timed transfers at pulse points are rejected.** Every TheRide transfer
  declares `min_transfer_time = 10 s` across bays up to 71 m apart — 25 km/h on
  foot — so a 60 s floor is applied instead. TheRide does hold connecting buses
  at Blake Transit Center, but GTFS has no way for them to say so in the data
  they publish, so nothing distinguishes a held connection from a coincidental
  one.
- **Boardings are bounded by a six-hour horizon.** A ride boarded inside it may
  finish outside it; cutting a trip off mid-ride would report "unreachable" for
  a bus the rider is already on.

## Service dates are not calendar days

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
