import type { ApiHealth } from "../useApiHealth";

interface Props {
  health: ApiHealth;
  vehicleCount: number;
  vehiclesLive: boolean;
}

/**
 * What the backend is doing, in one pill.
 *
 * Every state here is honest about a real condition rather than reassuring:
 * a free tier that has gone to sleep says so, and realtime that is unavailable
 * says "schedule only" instead of showing a green dot next to zero buses.
 */
export function StatusPill({ health, vehicleCount, vehiclesLive }: Props) {
  if (health.state === "checking") {
    return (
      <span className="status">
        <span className="status__pill status__pill--idle">
          <span className="spinner" aria-hidden />
          Connecting
        </span>
      </span>
    );
  }

  if (health.state === "waking") {
    return (
      <span className="status" aria-live="polite">
        <span className="status__pill status__pill--warn">
          <span className="spinner" aria-hidden />
          Waking the server · {health.seconds}s
        </span>
      </span>
    );
  }

  if (health.state === "down") {
    return (
      <span className="status" role="alert">
        <span className="status__pill status__pill--down">
          <span className="status__dot" aria-hidden />
          API unreachable
        </span>
      </span>
    );
  }

  // Connected with nothing to show is not "live": it means the poller is not
  // running or every bus is in the depot. A green dot beside "0 buses" says
  // the opposite of what is true.
  const reporting = vehiclesLive && vehicleCount > 0;

  return (
    <span className="status">
      <span
        className={`status__pill ${reporting ? "status__pill--live" : "status__pill--idle"}`}
      >
        <span className="status__dot" aria-hidden />
        {reporting ? `${vehicleCount} buses live` : "Schedule only"}
      </span>
      <span className="status__version">v{health.version}</span>
    </span>
  );
}
