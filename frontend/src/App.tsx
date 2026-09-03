import { ATTRIBUTION } from "./config";
import { TransitMap } from "./TransitMap";
import { useApiHealth } from "./useApiHealth";

function ApiStatus() {
  const health = useApiHealth();

  switch (health.state) {
    case "checking":
      return <span className="status status--pending">checking API…</span>;
    case "up":
      return <span className="status status--up">API v{health.version}</span>;
    case "down":
      return <span className="status status--down">API unreachable ({health.reason})</span>;
  }
}

export default function App() {
  return (
    <div className="app">
      <header className="header">
        <h1 className="header__title">a2transit</h1>
        <p className="header__tagline">TheRide + U-M MBus, as one network</p>
        <ApiStatus />
      </header>

      <main className="main">
        <aside className="panel">
          {/* M6 replaces this with origin/destination search and the itinerary list. */}
          <p className="panel__placeholder">
            Trip planning arrives in M6. The map below confirms MapLibre renders free
            OpenFreeMap tiles with no API key.
          </p>
        </aside>
        <TransitMap />
      </main>

      <footer className="footer">{ATTRIBUTION}</footer>
    </div>
  );
}
