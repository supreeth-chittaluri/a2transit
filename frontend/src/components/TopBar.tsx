import type { ReactNode } from "react";

/** Slim bar floating over the map: wordmark left, status right. */
export function TopBar({ children }: { children: ReactNode }) {
  return (
    <header className="topbar">
      <div className="wordmark">
        <h1 className="wordmark__name">a2transit</h1>
        <p className="wordmark__tagline">TheRide + U&#8209;M MBus, as one network</p>
      </div>
      <div className="topbar__end">{children}</div>
    </header>
  );
}
