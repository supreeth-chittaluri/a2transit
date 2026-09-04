# Licences: the code and the data are different things

The code in this repository is MIT — see [LICENSE](../LICENSE). It applies to
the source and to nothing else.

**No transit data is redistributed here.** The feeds are downloaded at runtime
from each agency and `data/` is git-ignored precisely so that stays true. If you
run this, you are consuming those feeds directly and their terms apply to you
rather than to me.

## AAATA / TheRide

A "limited, nonexclusive, non-assignable, nontransferable, revocable license"
to use and create derivative works of their data, "for the sole purpose of
assisting mass transportation riders or in furtherance of promoting public
transportation." Three obligations come with it:

1. **Display the attribution prominently** wherever their data appears:

   > Transit scheduling, geographic, and real-time data provided by permission
   > of AAATA/TheRide.

   The app renders this in its footer on every screen, and the API returns it in
   the body of every `/plan` response — so a client that only ever touches the
   API still receives the notice it has to display.

2. **Refresh the GTFS within three business days** of a new publication. The
   ingest is idempotent and skips unchanged feeds, so a weekly cron entry
   satisfies this at almost no cost:

   ```
   0 4 * * 1  cd /path/to/a2transit/backend && .venv/bin/python -m a2transit.ingest
   ```

3. **Do not use AAATA/TheRide logos or other intellectual property.** None are
   used here; route colours come from `routes.txt`, which is data.

"Nontransferable" is the reason the feed ZIPs are not committed to this
repository, and why the licence for the code says nothing about the data.

## University of Michigan Transit Services (MBus)

Published through the Mobility Database and Transitland without a stated
restriction. Attributed in the same footer as a matter of courtesy.

## TheRide's realtime endpoints

Undocumented, and listed in no feed registry — see
[feeds.md](feeds.md#provenance-of-the-realtime-urls--read-this-before-depending-on-them)
for how they were traced. They serve unauthenticated, spec-compliant
GTFS-Realtime v2.0 over public HTTPS, which is consuming an open feed rather
than scraping a page, but they carry no stability promise. Everything that
depends on them degrades to schedule-only routing when they are unavailable,
and the readiness endpoint reports that as `degraded` rather than as a failure.

Worth emailing `Developers@TheRide.org` before depending on them in anything
that matters.

## Map tiles

© OpenFreeMap and © OpenMapTiles, from OpenStreetMap data © OpenStreetMap
contributors, under the Open Database License. No key and no account, which is
why they are here rather than Mapbox.
