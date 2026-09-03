# Transit feeds: endpoints, formats, and licence

All endpoints below were probed live on **2026-09-03**. Re-check them with:

```bash
python scripts/verify_feeds.py
```

Everything here is a free, open, unauthenticated feed. No API keys, no accounts,
no billing. Nothing in this project requires a credit card on file.

---

## TheRide (Ann Arbor Area Transportation Authority)

`agency_id` **1179** · `agency_name` "AAATA" · timezone `America/New_York`

| Feed | URL | Format |
|---|---|---|
| GTFS Schedule | `https://www.theride.org/sites/default/files/google/google_transit.zip` | ZIP, ~2.50 MB |
| GTFS-RT VehiclePositions | `https://rt.theride.org/gtfsrt/vehicles` | protobuf, `application/x-google-protobuf` |
| GTFS-RT TripUpdates | `https://rt.theride.org/gtfsrt/trips` | protobuf |
| GTFS-RT Alerts | `https://rt.theride.org/gtfsrt/alerts` | protobuf |

### Provenance of the static URL

The URL widely cited online — `theride.org/google/google_transit.zip` — **now 404s**.
The Mobility Database entry that lists it (`tfs-147`) is marked superseded; the
current official entry is [`mdb-415`](https://mobilitydatabase.org/feeds/gtfs/mdb-415),
which gives the `/sites/default/files/` path above. Transitland
([`f-dps2-annarborareatransportationauthority`](https://www.transit.land/feeds/f-dps2-annarborareatransportationauthority))
agrees.

### Provenance of the realtime URLs — read this before depending on them

**These endpoints are not published in any feed registry.** Transitland and the
Mobility Database list *no* GTFS-Realtime feed for AAATA, and TheRide's
[developer page](https://www.theride.org/business/software-developers) documents
only the static ZIP, directing API requests to `Developers@TheRide.org`.

They were located by observing that TheRide's public "Track A Bus" map calls a
Clever Devices *BusTime* backend (its JSON responses are wrapped in
`{"bustime-response": ...}`), then checking the standard BusTime GTFS-RT path on
their realtime host. That is the same vendor and the same path layout U-M MBus
uses, and MBus's equivalents *are* registry-listed.

What this means in practice:

* They serve **spec-compliant GTFS-Realtime v2.0 protobuf** over public HTTPS with
  no authentication — this is consuming an open feed, not scraping a web page.
* But they are **undocumented**, so they carry no stability promise and could move
  without notice. The poller must degrade gracefully to schedule-only routing
  when realtime is unavailable (see M7).
* Worth emailing `Developers@TheRide.org` to confirm they are fair game and ask
  to be told about changes.

### Licence and obligations

TheRide grants a "limited, nonexclusive, non-assignable, nontransferable,
revocable license" to use and create derivative works of their data "for the sole
purpose of assisting mass transportation riders or in furtherance of promoting
public transportation." Conditions this project must meet:

1. **Display the attribution prominently**:
   `Transit scheduling, geographic, and real-time data provided by permission of AAATA/TheRide`
2. **Refresh the GTFS data within three business days** of a new file being
   published — the M1 refresh job must run at least weekly.
3. **Do not use AAATA/TheRide logos or other intellectual property.**

---

## U-M MBus (University of Michigan Transit Services)

`agency_id` **50158** · timezone `America/Detroit`

| Feed | URL | Format |
|---|---|---|
| GTFS Schedule | `https://webapps.fo.umich.edu/transit_uploads/google_transit.zip` | ZIP, ~1.12 MB |
| GTFS-RT VehiclePositions | `https://mbus.ltp.umich.edu/gtfsrt/vehicles` | protobuf |
| GTFS-RT TripUpdates | `https://mbus.ltp.umich.edu/gtfsrt/trips` | protobuf |
| GTFS-RT Alerts | `https://mbus.ltp.umich.edu/gtfsrt/alerts` | protobuf |

The older `ltp.umich.edu/gtfs/google_transit.zip` (Mobility Database `mdb-416`,
last fetched 2022) is superseded by [`mdb-2072`](https://mobilitydatabase.org/feeds/gtfs/mdb-2072).
The realtime endpoints are registry-listed under Transitland Onestop ID
[`f-dps2w-universityofmichigantransitservices~rt`](https://www.transit.land/feeds/f-dps2w-universityofmichigantransitservices~rt).

MBus runs the **same Clever Devices BusTime stack** as TheRide, so both agencies'
realtime payloads have identical structure. One poller implementation handles
both; only the base URL differs.

---

## Refresh behaviour

The two agencies handle conditional GETs differently, verified 2026-09-03:

| | `If-None-Match` / `If-Modified-Since` | Notes |
|---|---|---|
| MBus | **honoured** — returns `304 Not Modified` | Nothing transfers on an unchanged refresh. |
| TheRide | **ignored** — returns `200` with the full body | Serves a valid `ETag` (`"6a8ae767-2625db"`) and `Last-Modified`, but their Pantheon/Varnish front end resends the body regardless. |

So the ingest cannot rely on 304 alone to avoid needless work. It also records a
sha256 of every loaded ZIP in `feed_versions` and skips the database reload when
the content is unchanged, which is what actually protects the 200k-row load. The
2.4 MB re-download from TheRide is the residual cost, once a week.

## What the feeds actually contain

Static feed scale (2026-08-23 publication, both agencies):

| | TheRide | MBus |
|---|---:|---:|
| stops | 1,055 | 120 |
| routes | 30 | 16 |
| trips | 4,235 | 8,432 |
| stop_times | 106,066 | 108,658 |
| calendar | 3 | 11 |
| calendar_dates | 4 | 314 |
| transfers | 17 | 0 |
| service window | 2026-08-23 → 2027-01-30 | 2026-08-23 → 2027-01-02 |

Both feeds also ship `shapes.txt`, `fare_attributes.txt`, `fare_rules.txt`, and
`feed_info.txt`. Neither uses `frequencies.txt`, so every trip is explicitly
scheduled — the routing engine does not need frequency expansion.

Realtime snapshot taken 2026-09-03 16:08 UTC:

| | TheRide | MBus |
|---|---:|---:|
| vehicle positions | 71 (66 carrying `trip_id`) | 36 (23 carrying `trip_id`) |
| trip updates | 157 | 264 |
| alerts | 18 | 5 |

`VehiclePosition` carries `latitude`, `longitude`, `bearing`, `speed`, a vehicle
`id`, and a per-vehicle `timestamp`. Not every vehicle is matched to a trip — a
bus deadheading or off-route reports position only, so the map layer must handle
untripped vehicles.

---

## Two findings that constrain the schema

### 1. Stop and trip IDs collide across agencies, and the collisions are meaningless

90 `stop_id` values and 800 `trip_id` values appear in **both** feeds. They are
not the same objects. Of the 90 colliding stop IDs, exactly **one** pair is
co-located; the rest are kilometres apart:

```
stop_id 161 · TheRide "Tyler + Zephyr"           vs MBus "TEST STOP 1"    14,853 m apart
stop_id 162 · TheRide "Pauline + Stadium"        vs MBus "TEST STOP 2"     2,273 m apart
stop_id 190 · TheRide "Wiard + MacIntosh"        vs MBus "Huron Pkwy/Baxter NB"  12,022 m apart
```

**Consequence:** every primary and foreign key in the schema is composite —
`(agency_source, stop_id)`, `(agency_source, trip_id)`, and so on. This applies
to realtime too: a `trip_id` arriving on a GTFS-RT feed is ambiguous until it is
tagged with the agency it came from. Getting this wrong silently routes riders
onto the wrong agency's bus.

(`route_id` happens not to collide today — TheRide uses numerics like `4`, MBus
uses letter codes like `BB` and `CS` — but it gets the same composite treatment,
because that is a coincidence of the current feeds, not a guarantee.)

### 2. The cross-agency transfer premise is real

**728 TheRide↔MBus stop pairs lie within 400 m** of each other, and many are
effectively the same corner:

```
 0.4 m  TheRide "Bonisteel + Beal"             <-> MBus "Cooley Lab Outbound"
 0.7 m  TheRide "State + Monroe"               <-> MBus "Law Quad"
 1.4 m  TheRide "SB State + Monroe"            <-> MBus "South Quad"
 1.7 m  TheRide "NB Observatory + N University Ct" <-> MBus "Stockwell Hall Inbound"
 2.3 m  TheRide "Glen + Catherine"             <-> MBus "Glen/Catherine Outbound"
```

Measured with PostGIS `ST_DWithin` over `geography` after ingest (M1), which is
what M4 will actually generate footpaths from. An earlier estimate said 732; that
count used spherical haversine, and four pairs sit close enough to the 400 m
threshold that the WGS84 spheroid moves them across it. Where the two disagree,
the PostGIS number is the one that matters.

This is what makes the merged graph worth building: neither agency's own trip
planner will route you across these pairs. The PostGIS `ST_DWithin` footpath
generation in M4 will pick them up automatically.

---

## Other services used

| Service | Purpose | Cost |
|---|---|---|
| [OpenFreeMap](https://openfreemap.org/) / [Protomaps](https://protomaps.com/) | basemap tiles | free, no key |
| [Photon](https://photon.komoot.io/) | geocoding (address → lat/lon) | free, no key |
| [Nominatim](https://operations.osmfoundation.org/policies/nominatim/) | geocoding fallback — **usage policy caps 1 req/s, requires a real User-Agent** | free |
| [OSRM demo server](https://router.project-osrm.org/) | walking legs (M4, optional) — rate-limited, no SLA | free |

Walking legs start as straight-line haversine with a fixed speed; OSRM is an
enhancement, never a hard dependency.
