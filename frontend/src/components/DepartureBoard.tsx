import { useEffect, useState } from "react";

import { departures, type Departure } from "../lib/api";
import { formatClock, legColor } from "../lib/endpoints";

interface Props {
  stopId: string;
  stopName: string;
  at: string;
  onClose: () => void;
}

/** Minutes, or "due" — nobody reads "in 47 seconds". */
function countdown(seconds: number): string {
  const minutes = Math.round(seconds / 60);
  if (minutes <= 0) return "due";
  if (minutes < 60) return `${minutes} min`;
  return `${Math.floor(minutes / 60)} h ${String(minutes % 60).padStart(2, "0")}`;
}

export function DepartureBoard({ stopId, stopName, at, onClose }: Props) {
  const [state, setState] = useState<
    { kind: "loading" } | { kind: "ready"; rows: Departure[] } | { kind: "error" }
  >({ kind: "loading" });

  useEffect(() => {
    const controller = new AbortController();
    setState({ kind: "loading" });
    departures(stopId, at, controller.signal)
      .then((rows) => setState({ kind: "ready", rows }))
      .catch(() => {
        if (!controller.signal.aborted) setState({ kind: "error" });
      });
    return () => controller.abort();
  }, [stopId, at]);

  return (
    <section className="board" aria-label={`Departures from ${stopName}`}>
      <header className="board__header">
        <h2 className="board__title">{stopName}</h2>
        <button type="button" className="board__close" onClick={onClose} aria-label="Close">
          ×
        </button>
      </header>

      {state.kind === "loading" && <p className="panel__placeholder">Loading departures…</p>}
      {state.kind === "error" && (
        <p className="panel__error">Could not load departures for this stop.</p>
      )}
      {state.kind === "ready" && state.rows.length === 0 && (
        <p className="panel__placeholder">Nothing more leaves this stop today.</p>
      )}
      {state.kind === "ready" && state.rows.length > 0 && (
        <ol className="board__rows">
          {state.rows.map((row) => (
            <li key={`${row.tripId}-${row.departure}`} className="board__row">
              <span
                className="route-badge"
                style={{ background: legColor(row.routeColor, row.agency) }}
              >
                {row.routeLabel}
              </span>
              <span className="board__headsign">{row.headsign}</span>
              <span className="board__when">
                <span className="board__countdown">{countdown(row.inSeconds)}</span>
                <span className="board__clock">{formatClock(row.departure)}</span>
              </span>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
