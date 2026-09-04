import { useCallback, useEffect, useMemo, useState } from "react";

import { ATTRIBUTION } from "./config";
import { AlertBanner } from "./components/AlertBanner";
import { DepartureBoard } from "./components/DepartureBoard";
import { EndpointField } from "./components/EndpointField";
import { ItineraryList } from "./components/ItineraryList";
import { TransitMap } from "./TransitMap";
import { ApiError, planTrip, type PlanResponse } from "./lib/api";
import { toLocalIso, toQueryValue, type Endpoint } from "./lib/endpoints";
import { readTripFromUrl, writeTripToUrl } from "./lib/tripUrl";
import { useVehicles } from "./lib/useVehicles";
import { useApiHealth } from "./useApiHealth";

/**
 * The feeds cover 2026-08-23 to 2027-01-30, so "now" is only a useful default
 * while today falls inside that. Outside it every query correctly returns
 * nothing, which reads as a broken planner rather than an expired timetable.
 */
const FEED_START = "2026-08-23";
const FEED_END = "2027-01-02";

function clampToFeedWindow(iso: string): string {
  if (iso.slice(0, 10) < FEED_START) return `${FEED_START}T09:00`;
  if (iso.slice(0, 10) > FEED_END) return `${FEED_END}T09:00`;
  return iso;
}

const nowInWindow = () => clampToFeedWindow(toLocalIso(new Date()));

function ApiStatus({ vehicleCount, live }: { vehicleCount: number; live: boolean }) {
  const health = useApiHealth();

  if (health.state === "checking") {
    return <span className="status status--pending">checking API…</span>;
  }
  if (health.state === "waking") {
    // The free tier sleeps after fifteen minutes idle. Saying so beats a red
    // "unreachable" that makes a working planner look broken to whoever just
    // opened the link.
    return (
      <span className="status status--pending" aria-live="polite">
        ◌ waking the server… {health.seconds}s
      </span>
    );
  }
  if (health.state === "down") {
    return <span className="status status--down">API unreachable ({health.reason})</span>;
  }
  // Connected but with nothing to show is not "live". It means the poller is
  // not running, or every bus is in the depot — either way, claiming
  // "0 buses live" next to a green dot says the opposite of what is true.
  const reporting = live && vehicleCount > 0;
  return (
    <span className="status">
      {/* Realtime being off is a normal state, not a fault: the planner works
          from the schedule and says so rather than pretending. */}
      <span className={reporting ? "status--up" : "status--pending"}>
        {reporting ? `● ${vehicleCount} buses live` : "○ schedule only"}
      </span>
      <span className="status__version">API v{health.version}</span>
    </span>
  );
}

type PlanState =
  | { state: "idle" }
  | { state: "loading" }
  | { state: "done"; response: PlanResponse }
  | { state: "error"; message: string };

export default function App() {
  // Seeded from the URL, so a shared link opens the trip it describes.
  const initial = useMemo(() => readTripFromUrl(), []);
  const [origin, setOrigin] = useState<Endpoint | null>(initial.origin);
  const [destination, setDestination] = useState<Endpoint | null>(initial.destination);
  const [depart, setDepart] = useState(() =>
    initial.depart ? clampToFeedWindow(initial.depart) : nowInWindow(),
  );
  const [plan, setPlan] = useState<PlanState>({ state: "idle" });
  const [selected, setSelected] = useState(0);
  const [openField, setOpenField] = useState<"origin" | "destination" | null>(null);
  const [board, setBoard] = useState<{ id: string; name: string } | null>(null);
  const [copied, setCopied] = useState(false);
  const feed = useVehicles();

  // Picking on the map fills whichever end is still empty — destination first,
  // because an origin is usually typed and a destination is usually pointed at.
  const pickOnMap = useCallback(
    (lat: number, lon: number) => {
      const place: Endpoint = {
        kind: "place",
        label: `${lat.toFixed(5)}, ${lon.toFixed(5)}`,
        lat,
        lon,
      };
      if (!origin) setOrigin(place);
      else if (!destination) setDestination(place);
    },
    [origin, destination],
  );

  useEffect(() => {
    writeTripToUrl({ origin, destination, depart });
  }, [origin, destination, depart]);

  useEffect(() => {
    if (!origin || !destination) {
      setPlan({ state: "idle" });
      return;
    }

    const controller = new AbortController();
    setPlan({ state: "loading" });
    setSelected(0);

    planTrip(toQueryValue(origin), toQueryValue(destination), depart, controller.signal)
      .then((response) => setPlan({ state: "done", response }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setPlan({
          state: "error",
          message:
            error instanceof ApiError
              ? error.message
              : "Could not reach the planner — it may still be waking up. " +
                "The free tier sleeps when idle; this usually clears in under a minute.",
        });
      });

    return () => controller.abort();
  }, [origin, destination, depart]);

  // A link shared from the URL bar names a stop by id; the plan response names
  // it properly, so the fields fill in once the answer arrives.
  useEffect(() => {
    if (plan.state !== "done") return;
    setOrigin((current) =>
      current && current.kind === "stop" && current.label === current.id
        ? { ...current, label: plan.response.origin.name, lat: plan.response.origin.lat, lon: plan.response.origin.lon }
        : current,
    );
    setDestination((current) =>
      current && current.kind === "stop" && current.label === current.id
        ? {
            ...current,
            label: plan.response.destination.name,
            lat: plan.response.destination.lat,
            lon: plan.response.destination.lon,
          }
        : current,
    );
  }, [plan]);

  const swap = () => {
    setOrigin(destination);
    setDestination(origin);
  };

  const share = async () => {
    try {
      await navigator.clipboard.writeText(window.location.href);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard access can be refused, and the URL is in the address bar
      // anyway — there is nothing useful to say about it.
    }
  };

  const itineraries = plan.state === "done" ? plan.response.itineraries : [];
  const shown = itineraries[selected] ?? null;
  const routeIds = useMemo(
    () =>
      new Set(
        itineraries.flatMap((itinerary) =>
          itinerary.legs.map((leg) => leg.routeId).filter((id): id is string => Boolean(id)),
        ),
      ),
    [itineraries],
  );

  return (
    <div className="app">
      <header className="header">
        <h1 className="header__title">a2transit</h1>
        <p className="header__tagline">TheRide + U&#8209;M MBus, as one network</p>
        <ApiStatus vehicleCount={feed.vehicles.length} live={feed.state === "live"} />
      </header>

      <main className="main">
        <aside className="panel">
          <form className="search" onSubmit={(event) => event.preventDefault()}>
            <EndpointField
              label="From"
              placeholder="Stop name or address"
              value={origin}
              onChange={setOrigin}
              isOpen={openField === "origin"}
              onOpenChange={(open) => setOpenField(open ? "origin" : null)}
            />
            <button
              type="button"
              className="swap"
              onClick={swap}
              disabled={!origin && !destination}
              aria-label="Swap origin and destination"
              title="Swap"
            >
              ⇅
            </button>
            <EndpointField
              label="To"
              placeholder="Stop name or address"
              value={destination}
              onChange={setDestination}
              isOpen={openField === "destination"}
              onOpenChange={(open) => setOpenField(open ? "destination" : null)}
            />
            <div className="field">
              <label className="field__label" htmlFor="depart">
                Leaving
              </label>
              <div className="field__row">
                <input
                  id="depart"
                  className="field__input"
                  type="datetime-local"
                  value={depart}
                  min={`${FEED_START}T00:00`}
                  max={`${FEED_END}T23:59`}
                  onChange={(event) => setDepart(event.target.value)}
                />
                <button
                  type="button"
                  className="chip"
                  onClick={() => setDepart(nowInWindow())}
                >
                  Now
                </button>
              </div>
            </div>
          </form>

          <div className="results">
            {board && (
              <DepartureBoard
                stopId={board.id}
                stopName={board.name}
                at={depart}
                onClose={() => setBoard(null)}
              />
            )}

            <AlertBanner routeIds={routeIds} />

            {plan.state === "idle" && (
              <p className="panel__placeholder">
                Pick an origin and a destination — type a stop or an address, or click
                the map. Journeys cross between the two agencies wherever their stops
                are within a 400&nbsp;m walk, which neither agency's own planner will
                do for you.
              </p>
            )}
            {plan.state === "loading" && (
              <p className="panel__placeholder" aria-live="polite">
                Planning…
              </p>
            )}
            {plan.state === "error" && (
              <p className="panel__error" role="alert">
                {plan.message}
              </p>
            )}
            {plan.state === "done" && itineraries.length === 0 && (
              <p className="panel__placeholder">
                Nothing runs between these two within six hours of that time. Service
                is thin before 06:00 and after the last run, and thinner on Sundays —
                try a different departure.
              </p>
            )}
            {/* Only when live data actually changed something. A query for a
                future date legitimately has predictions applied and nothing
                adjusted, and "Live: 0 trips adjusted" is a claim about the
                plumbing rather than about the rider's trip. */}
            {plan.state === "done" && plan.response.realtime.runsAdjusted > 0 && (
              <p className="realtime-note">
                Live: {plan.response.realtime.runsAdjusted} trips adjusted, worst
                delay {Math.round(plan.response.realtime.maxDelaySeconds / 60)} min.
              </p>
            )}
            {plan.state === "done" && itineraries.length > 0 && (
              <>
                <ItineraryList
                  itineraries={itineraries}
                  selected={selected}
                  onSelect={setSelected}
                  queryMs={plan.response.queryMs}
                  destinationLabel={destination?.label ?? ""}
                  onShowDepartures={(id, name) => setBoard({ id, name })}
                />
                <button type="button" className="share" onClick={share}>
                  {copied ? "Link copied" : "Copy link to this trip"}
                </button>
              </>
            )}
          </div>
        </aside>

        <TransitMap itinerary={shown} vehicles={feed.vehicles} onPick={pickOnMap} />
      </main>

      <footer className="footer">{ATTRIBUTION}</footer>
    </div>
  );
}
