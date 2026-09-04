import type { ReactNode } from "react";

/**
 * Every non-answer the panel can show.
 *
 * Each is designed rather than left as a sentence of unstyled text, because
 * these are what a visitor sees first: an empty planner, a slow one, a broken
 * one, or one whose free-tier host has gone to sleep.
 */

function State({
  variant,
  icon,
  title,
  children,
}: {
  variant?: "error" | "waking";
  icon: ReactNode;
  title: string;
  children: ReactNode;
}) {
  return (
    <div className={`state${variant ? ` state--${variant}` : ""}`}>
      <span className="state__icon" aria-hidden>
        {icon}
      </span>
      <span className="state__title">{title}</span>
      <p className="state__body">{children}</p>
    </div>
  );
}

export const IdleState = () => (
  <State icon="◎" title="Where are you going?">
    Type a stop or an address into either field, or click the map. Journeys cross
    between the two agencies wherever their stops are within a 400&nbsp;m walk —
    which neither agency's own planner will do for you.
  </State>
);

export const NoResultsState = () => (
  <State icon="○" title="Nothing runs between these two">
    Not within six hours of that time. Service is thin before 06:00 and after the
    last run, and thinner on Sundays — try a different departure.
  </State>
);

export const ErrorState = ({ message }: { message: string }) => (
  <div role="alert">
    <State variant="error" icon="!" title="Could not plan that trip">
      {message}
    </State>
  </div>
);

export const WakingState = ({ seconds }: { seconds: number }) => (
  <div aria-live="polite">
    <State variant="waking" icon="◌" title="Waking the server">
      The demo runs on a free tier that sleeps after fifteen minutes idle. This
      usually takes under a minute — {seconds}s so far — and the planner will
      appear on its own.
    </State>
  </div>
);

/** Loading looks like the answer, so the panel does not jump when it lands. */
export const ResultsSkeleton = () => (
  <div className="results" aria-busy="true" aria-label="Planning">
    {[0, 1].map((i) => (
      <div className="skeleton-card" key={i}>
        <div className="skeleton" style={{ height: 26, width: "45%" }} />
        <div className="skeleton" style={{ height: 12, width: "70%" }} />
        <div className="skeleton" style={{ height: 8, width: "100%" }} />
      </div>
    ))}
    <span className="visually-hidden">Planning your trip…</span>
  </div>
);
