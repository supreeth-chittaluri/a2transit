import { useEffect, useState } from "react";

import { serviceAlerts, type Alert } from "../lib/api";

interface Props {
  /** Route ids used by the plan on screen. */
  routeIds: Set<string>;
}

/** Alerts shown open. More than this and the itinerary is below the fold. */
const MAX_PROMINENT = 2;

/**
 * Service alerts, ranked by whether they touch the journey on screen.
 *
 * The two agencies publish 23 alerts between them and most name no route at
 * all: a temporary stop relocation, Labor Day service, and — genuinely — an MBus
 * driver recruitment notice. Treating "names no route" as "network-wide,
 * therefore important" put five paragraphs of hiring copy above the itinerary,
 * and dismissing them just promoted the next five.
 *
 * So the ranking is: an alert naming a route the plan actually uses is shown
 * open, because it is the one that might change the trip. Everything else
 * collapses into a single line with a count, which is honest — the alerts are
 * there, they are one click away — without turning a journey planner into an
 * alerts reader.
 */
export function AlertBanner({ routeIds }: Props) {
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [dismissed, setDismissed] = useState<Set<string>>(new Set());
  const [showOthers, setShowOthers] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    serviceAlerts(controller.signal)
      .then(setAlerts)
      // No alerts is indistinguishable from no poller running, and neither is
      // worth interrupting a rider over.
      .catch(() => setAlerts([]));
    return () => controller.abort();
  }, []);

  const live = alerts.filter((alert) => !dismissed.has(alert.id));
  const relevant = live.filter((alert) => alert.routeIds.some((id) => routeIds.has(id)));
  const others = live.filter((alert) => !relevant.includes(alert));

  if (live.length === 0) return null;

  const dismiss = (id: string) =>
    setDismissed((current) => new Set(current).add(id));

  return (
    <div className="alerts">
      {relevant.slice(0, MAX_PROMINENT).map((alert) => (
        <AlertCard key={alert.id} alert={alert} onDismiss={() => dismiss(alert.id)} />
      ))}

      {others.length > 0 && (
        <div className="alerts__others">
          <button
            type="button"
            className="alerts__toggle"
            aria-expanded={showOthers}
            onClick={() => setShowOthers((open) => !open)}
          >
            {showOthers ? "▾" : "▸"} {others.length} other service alert
            {others.length === 1 ? "" : "s"}
          </button>
          {showOthers &&
            others.map((alert) => (
              <AlertCard
                key={alert.id}
                alert={alert}
                onDismiss={() => dismiss(alert.id)}
              />
            ))}
        </div>
      )}
    </div>
  );
}

function AlertCard({ alert, onDismiss }: { alert: Alert; onDismiss: () => void }) {
  return (
    <div className="alert" role="status">
      <div className="alert__body">
        <strong className="alert__header">{alert.header}</strong>
        {alert.description && <p className="alert__text">{alert.description}</p>}
        {alert.url && (
          <a className="alert__link" href={alert.url} target="_blank" rel="noreferrer">
            More
          </a>
        )}
      </div>
      <button
        type="button"
        className="alert__dismiss"
        aria-label={`Dismiss: ${alert.header}`}
        onClick={onDismiss}
      >
        ×
      </button>
    </div>
  );
}
