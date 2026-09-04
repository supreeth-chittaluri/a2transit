import type { Itinerary, Leg } from "../lib/api";
import { legColor } from "../lib/agency";
import { formatClock, formatDuration } from "../lib/endpoints";

interface Props {
  itinerary: Itinerary;
  index: number;
  selected: boolean;
  onSelect: () => void;
  destinationLabel: string;
  onShowDepartures: (stopId: string, stopName: string) => void;
}

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

/**
 * The whole journey as one proportional bar.
 *
 * Two options are usually "one change, slower" versus "two changes, faster",
 * and the strip answers that before either card is read: the walk hatching and
 * the agency colours show at a glance how much of the trip is on foot and
 * whose bus does the work.
 */
function LegStrip({ legs }: { legs: Leg[] }) {
  const total = legs.reduce((sum, leg) => sum + leg.seconds, 0) || 1;
  return (
    <div className="legstrip" aria-hidden>
      {legs.map((leg, i) => (
        <span
          key={i}
          className={`legstrip__seg legstrip__seg--${
            leg.kind === "walk" ? "walk" : leg.agency === "mbus" ? "mbus" : "theride"
          }`}
          style={{
            flexGrow: leg.seconds / total,
            ...(leg.kind === "ride"
              ? { background: legColor(leg.routeColor, leg.agency) }
              : null),
          }}
        />
      ))}
    </div>
  );
}

function LegRow({
  leg,
  isLast,
  destinationLabel,
  onShowDepartures,
}: {
  leg: Leg;
  isLast: boolean;
  destinationLabel: string;
  onShowDepartures: (stopId: string, stopName: string) => void;
}) {
  if (leg.kind === "walk") {
    const sameStop = leg.fromStop.name === leg.toStop.name;
    // A place has no name in the feed, so the API can only echo the coordinates
    // it was given; the browser is the only party that knows what was typed.
    const to = isLast && leg.toStop.id === null ? destinationLabel : leg.toStop.name;
    return (
      <li className="leg leg--walk">
        <span className="leg__time">{formatClock(leg.depart)}</span>
        <span className="leg__rail" aria-hidden>
          <span className="leg__node" />
        </span>
        <span className="leg__body">
          <span className="leg__headline">
            <span className="leg__stop">{sameStop ? "Wait here" : `Walk to ${to}`}</span>
          </span>
          {/* The leg spans the walk *and* the wait for the next vehicle, so it
              is never described as walking time alone. */}
          <span className="leg__detail">{formatDuration(leg.seconds)} including wait</span>
        </span>
      </li>
    );
  }

  const color = legColor(leg.routeColor, leg.agency);
  return (
    <li className="leg leg--ride" style={{ "--track-color": color } as React.CSSProperties}>
      <span className="leg__time">{formatClock(leg.depart)}</span>
      <span className="leg__rail" aria-hidden>
        <span className="leg__node" />
      </span>
      <span className="leg__body">
        <span className="leg__headline">
          <span className="route-badge" style={{ "--badge-bg": color } as React.CSSProperties}>
            {leg.routeLabel}
          </span>
          {leg.fromStop.id ? (
            <button
              type="button"
              className="leg__stop-button"
              onClick={() => onShowDepartures(leg.fromStop.id!, leg.fromStop.name)}
              title="What else leaves from this stop"
            >
              {leg.fromStop.name}
            </button>
          ) : (
            <span className="leg__stop">{leg.fromStop.name}</span>
          )}
        </span>
        <span className="leg__detail">
          {leg.headsign ? `towards ${leg.headsign} · ` : ""}
          {plural((leg.intermediateStops ?? 0) + 1, "stop")} · {formatDuration(leg.seconds)}
        </span>
        <span className="leg__detail">
          {formatClock(leg.arrive)} — {leg.toStop.name}
        </span>
      </span>
    </li>
  );
}

export function ItineraryCard({
  itinerary,
  index,
  selected,
  onSelect,
  destinationLabel,
  onShowDepartures,
}: Props) {
  const rides = itinerary.legs.filter((leg) => leg.kind === "ride");
  const panelId = `itinerary-${index}-legs`;

  return (
    <article className={`card${selected ? " card--selected" : ""}`}>
      <button
        type="button"
        className="card__summary"
        aria-expanded={selected}
        aria-controls={panelId}
        onClick={onSelect}
      >
        <span className="card__duration">{formatDuration(itinerary.durationSeconds)}</span>
        <span className="card__times">
          {formatClock(itinerary.departure)} → {formatClock(itinerary.arrival)}
        </span>

        <span className="card__meta">
          <span>
            {itinerary.rideCount === 0
              ? "Walk the whole way"
              : plural(itinerary.transfers, "change")}
          </span>
          {itinerary.walkSeconds > 0 && (
            <>
              <span className="card__meta-dot" aria-hidden>
                ·
              </span>
              <span>{formatDuration(itinerary.walkSeconds)} on foot</span>
            </>
          )}
          {rides.length > 0 && (
            <>
              <span className="card__meta-dot" aria-hidden>
                ·
              </span>
              {rides.map((leg, i) => (
                <span
                  key={i}
                  className="route-badge route-badge--sm"
                  style={
                    { "--badge-bg": legColor(leg.routeColor, leg.agency) } as React.CSSProperties
                  }
                >
                  {leg.routeLabel}
                </span>
              ))}
            </>
          )}
        </span>

        <LegStrip legs={itinerary.legs} />
      </button>

      {selected && (
        <ol className="legs" id={panelId}>
          {itinerary.legs.map((leg, i) => (
            <LegRow
              key={i}
              leg={leg}
              isLast={i === itinerary.legs.length - 1}
              destinationLabel={destinationLabel}
              onShowDepartures={onShowDepartures}
            />
          ))}
          <li className="leg">
            <span className="leg__time">{formatClock(itinerary.arrival)}</span>
            <span className="leg__rail" aria-hidden>
              <span className="leg__node" style={{ background: "var(--text)" }} />
            </span>
            <span className="leg__body">
              <span className="leg__stop">Arrive</span>
            </span>
          </li>
        </ol>
      )}
    </article>
  );
}
