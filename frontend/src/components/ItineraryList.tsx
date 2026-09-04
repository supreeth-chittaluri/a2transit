import type { Itinerary, Leg } from "../lib/api";
import { formatClock, formatDuration, legColor } from "../lib/endpoints";

interface Props {
  itineraries: Itinerary[];
  selected: number;
  onSelect: (index: number) => void;
  queryMs: number;
  /** What the rider called the destination; the API only has its coordinates. */
  destinationLabel: string;
  onShowDepartures: (stopId: string, stopName: string) => void;
}

const plural = (count: number, word: string) => `${count} ${word}${count === 1 ? "" : "s"}`;

function LegRow({
  leg,
  toLabel,
  onShowDepartures,
}: {
  leg: Leg;
  toLabel?: string;
  onShowDepartures?: (stopId: string, stopName: string) => void;
}) {
  if (leg.kind === "walk") {
    const isSameStop = leg.fromStop.name === leg.toStop.name;
    return (
      <li className="leg leg--walk">
        <span className="leg__time">{formatClock(leg.depart)}</span>
        <span className="leg__marker leg__marker--walk" aria-hidden />
        <span className="leg__body">
          {/* The leg spans the walk *and* the wait for the next vehicle, so it
              is never described as walking time. */}
          {isSameStop ? "Wait here" : `Walk to ${toLabel ?? leg.toStop.name}`}
          <span className="leg__detail">{formatDuration(leg.seconds)} incl. wait</span>
        </span>
      </li>
    );
  }

  const color = legColor(leg.routeColor, leg.agency);
  return (
    <li className="leg leg--ride">
      <span className="leg__time">{formatClock(leg.depart)}</span>
      <span className="leg__marker" style={{ background: color }} aria-hidden />
      <span className="leg__body">
        <span className="leg__headline">
          <span className="route-badge" style={{ background: color }}>
            {leg.routeLabel}
          </span>
          {/* The boarding stop opens its departure board: the question a rider
              standing at a stop actually has is "what else comes past here". */}
          {leg.fromStop.id && onShowDepartures ? (
            <button
              type="button"
              className="leg__from leg__from--button"
              onClick={() => onShowDepartures(leg.fromStop.id!, leg.fromStop.name)}
              title="Departures from this stop"
            >
              {leg.fromStop.name}
            </button>
          ) : (
            <span className="leg__from">{leg.fromStop.name}</span>
          )}
        </span>
        <span className="leg__detail">
          {leg.headsign ? `towards ${leg.headsign} · ` : ""}
          {plural((leg.intermediateStops ?? 0) + 1, "stop")} · {formatDuration(leg.seconds)}
        </span>
        <span className="leg__to">
          {formatClock(leg.arrive)} — {leg.toStop.name}
        </span>
      </span>
    </li>
  );
}

export function ItineraryList({
  itineraries,
  selected,
  onSelect,
  queryMs,
  destinationLabel,
  onShowDepartures,
}: Props) {
  return (
    <div className="itineraries">
      <p className="itineraries__summary">
        {plural(itineraries.length, "option")}
        {itineraries.length > 1 && " — fewest changes first, fastest last"}
        <span className="itineraries__timing">{queryMs.toFixed(1)} ms</span>
      </p>

      {itineraries.map((itinerary, index) => {
        const isSelected = index === selected;
        return (
          <article
            key={`${itinerary.departure}-${itinerary.arrival}-${index}`}
            className={`itinerary${isSelected ? " itinerary--selected" : ""}`}
          >
            <button
              type="button"
              className="itinerary__header"
              aria-expanded={isSelected}
              onClick={() => onSelect(index)}
            >
              <span className="itinerary__times">
                {formatClock(itinerary.departure)} → {formatClock(itinerary.arrival)}
              </span>
              <span className="itinerary__duration">
                {formatDuration(itinerary.durationSeconds)}
              </span>
              <span className="itinerary__meta">
                {itinerary.rideCount === 0
                  ? "walk"
                  : plural(itinerary.transfers, "change")}
                {itinerary.walkSeconds > 0 &&
                  ` · ${formatDuration(itinerary.walkSeconds)} on foot`}
              </span>
              <span className="itinerary__routes">
                {itinerary.legs
                  .filter((leg) => leg.kind === "ride")
                  .map((leg, legIndex) => (
                    <span
                      key={legIndex}
                      className="route-badge route-badge--small"
                      style={{ background: legColor(leg.routeColor, leg.agency) }}
                    >
                      {leg.routeLabel}
                    </span>
                  ))}
              </span>
            </button>

            {isSelected && (
              <ol className="legs">
                {itinerary.legs.map((leg, legIndex) => (
                  <LegRow
                    key={legIndex}
                    leg={leg}
                    onShowDepartures={onShowDepartures}
                    // A place has no name in the feed, so the API can only
                    // echo back the coordinates it was given. The browser is
                    // the only party that knows what the rider typed.
                    toLabel={
                      legIndex === itinerary.legs.length - 1 && leg.toStop.id === null
                        ? destinationLabel
                        : undefined
                    }
                  />
                ))}
                <li className="leg leg--arrival">
                  <span className="leg__time">{formatClock(itinerary.arrival)}</span>
                  <span className="leg__marker leg__marker--end" aria-hidden />
                  <span className="leg__body">Arrive</span>
                </li>
              </ol>
            )}
          </article>
        );
      })}
    </div>
  );
}
