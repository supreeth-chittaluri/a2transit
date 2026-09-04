import { useCallback, useEffect, useMemo, useState } from "react";

import { ATTRIBUTION } from "./config";
import { AlertBanner } from "./components/AlertBanner";
import { DepartureBoard } from "./components/DepartureBoard";
import { EndpointField } from "./components/EndpointField";
import { ItineraryList } from "./components/ItineraryList";
import { Panel } from "./components/Panel";
import { StatusPill } from "./components/StatusPill";
import { TopBar } from "./components/TopBar";
import {
  ErrorState,
  IdleState,
  NoResultsState,
  ResultsSkeleton,
  WakingState,
} from "./components/states";
import { useIsMobile } from "./hooks/useIsMobile";
import { useTripPlan } from "./hooks/useTripPlan";
import { toLocalIso, toQueryValue, type Endpoint } from "./lib/endpoints";
import { readTripFromUrl, writeTripToUrl } from "./lib/tripUrl";
import { useVehicles } from "./lib/useVehicles";
import { TransitMap } from "./map/TransitMap";
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

/** Matches --rail-width in tokens.css; the map insets its fit by this much. */
const RAIL_WIDTH = 400;
/** The sheet's mid snap point, so a fitted route clears it on a phone. */
const SHEET_FRACTION = 0.55;

export default function App() {
  // Seeded from the URL, so a shared link opens the trip it describes.
  const initial = useMemo(() => readTripFromUrl(), []);
  const [origin, setOrigin] = useState<Endpoint | null>(initial.origin);
  const [destination, setDestination] = useState<Endpoint | null>(initial.destination);
  const [depart, setDepart] = useState(() =>
    initial.depart ? clampToFeedWindow(initial.depart) : nowInWindow(),
  );
  const [openField, setOpenField] = useState<"origin" | "destination" | null>(null);
  const [selected, setSelected] = useState(0);
  const [board, setBoard] = useState<{ id: string; name: string } | null>(null);
  const [copied, setCopied] = useState(false);

  const isMobile = useIsMobile();
  const health = useApiHealth();
  const feed = useVehicles();
  const plan = useTripPlan(
    origin ? toQueryValue(origin) : null,
    destination ? toQueryValue(destination) : null,
    depart,
  );

  useEffect(() => {
    writeTripToUrl({ origin, destination, depart });
  }, [origin, destination, depart]);

  // A new answer means the previously selected index refers to a different
  // journey, or to none.
  useEffect(() => setSelected(0), [plan]);

  // A link shared from the URL bar names a stop by id; the plan response names
  // it properly, so the fields fill in once the answer arrives.
  useEffect(() => {
    if (plan.state !== "done") return;
    const fill = (
      current: Endpoint | null,
      resolved: { name: string; lat: number; lon: number },
    ) =>
      current && current.kind === "stop" && current.label === current.id
        ? { ...current, label: resolved.name, lat: resolved.lat, lon: resolved.lon }
        : current;
    setOrigin((c) => fill(c, plan.response.origin));
    setDestination((c) => fill(c, plan.response.destination));
  }, [plan]);

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
        itineraries.flatMap((it) =>
          it.legs.map((leg) => leg.routeId).filter((id): id is string => Boolean(id)),
        ),
      ),
    [itineraries],
  );

  return (
    <div className="app">
      <TopBar>
        <StatusPill
          health={health}
          vehicleCount={feed.vehicles.length}
          vehiclesLive={feed.state === "live"}
        />
      </TopBar>

      <Panel expandSignal={itineraries.length}>
        <div className="panel__body">
          <form className="fields" onSubmit={(event) => event.preventDefault()}>
            <EndpointField
              label="From"
              kind="origin"
              placeholder="Stop name or address"
              value={origin}
              onChange={setOrigin}
              isOpen={openField === "origin"}
              onOpenChange={(open) => setOpenField(open ? "origin" : null)}
            />

            <button
              type="button"
              className="field__swap"
              onClick={() => {
                setOrigin(destination);
                setDestination(origin);
              }}
              disabled={!origin && !destination}
            >
              <span aria-hidden>⇅</span> Swap
            </button>

            <EndpointField
              label="To"
              kind="destination"
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
                <span className="field__control">
                  <input
                    id="depart"
                    className="field__input"
                    type="datetime-local"
                    value={depart}
                    min={`${FEED_START}T00:00`}
                    max={`${FEED_END}T23:59`}
                    onChange={(event) => setDepart(event.target.value)}
                  />
                </span>
                <button
                  type="button"
                  className="chip-button"
                  onClick={() => setDepart(nowInWindow())}
                >
                  Now
                </button>
              </div>
            </div>
          </form>

          <div className="panel__scroll scroll-y">
            {board && (
              <div style={{ marginBottom: "var(--sp-3)" }}>
                <DepartureBoard
                  stopId={board.id}
                  stopName={board.name}
                  at={depart}
                  onClose={() => setBoard(null)}
                />
              </div>
            )}

            {/* Alerts are shown against a journey, not in the abstract. With
                no plan on screen every one of the 23 the agencies publish is
                "other", and a collapsed count of them is the first thing a
                visitor would see — noise before they have asked anything. */}
            {plan.state === "done" && <AlertBanner routeIds={routeIds} />}

            {/* Waking outranks idle: if the API is asleep, that is the thing
                the rider needs to know, not an invitation to type. */}
            {health.state === "waking" && plan.state !== "done" ? (
              <WakingState seconds={health.seconds} />
            ) : (
              <>
                {plan.state === "idle" && <IdleState />}
                {plan.state === "loading" && <ResultsSkeleton />}
                {plan.state === "error" && <ErrorState message={plan.message} />}
                {plan.state === "done" && itineraries.length === 0 && <NoResultsState />}
              </>
            )}

            {plan.state === "done" && plan.response.realtime.runsAdjusted > 0 && (
              <p className="realtime-note">
                <span className="status__dot" aria-hidden />
                Live: {plan.response.realtime.runsAdjusted} trips adjusted, worst delay{" "}
                {Math.round(plan.response.realtime.maxDelaySeconds / 60)} min
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
                <button
                  type="button"
                  className="share"
                  onClick={share}
                  style={{ marginTop: "var(--sp-2)" }}
                >
                  {copied ? "✓ Link copied" : "Copy link to this trip"}
                </button>
              </>
            )}
          </div>
        </div>

        {/* TheRide's licence requires this be displayed prominently wherever
            their data appears. See docs/licences.md. */}
        <p className="attribution">{ATTRIBUTION}</p>
      </Panel>

      {/* Last in the DOM, painted underneath.
          The map is the visual canvas but not the primary control, and the
          canvas MapLibre renders is focusable — it is keyboard-pannable, which
          is a real feature. First in the DOM meant the first Tab landed on the
          map, and a keyboard user traversed the canvas, the zoom buttons, the
          geolocate button and the attribution before reaching the search.
          Stacking is unaffected: the panel and top bar carry positive z-index,
          so they paint above regardless of order. */}
      <TransitMap
        itinerary={shown}
        vehicles={feed.vehicles}
        onPick={pickOnMap}
        padLeft={isMobile ? 0 : RAIL_WIDTH}
        padBottom={isMobile ? window.innerHeight * SHEET_FRACTION : 0}
      />
    </div>
  );
}
