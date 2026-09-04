import { useEffect, useRef, useState } from "react";

import { apiUrl } from "./config";

export type ApiHealth =
  | { state: "checking" }
  | { state: "waking"; seconds: number }
  | { state: "up"; version: string }
  | { state: "down"; reason: string };

/**
 * A free-tier API sleeps after fifteen minutes idle, and the request that wakes
 * it can take the better part of a minute. Reporting that as "API unreachable"
 * is both wrong and the worst possible thing to show the first person who opens
 * a link somebody sent them — the planner looks broken at exactly the moment it
 * is being judged.
 *
 * So a first failure is treated as "probably asleep" rather than "down": retry
 * with a rising delay, count the seconds, and let the UI say what is actually
 * happening. Only after RETRY_BUDGET_MS of no answer is it genuinely down.
 *
 * The distinction is honest in both directions. A local dev server that is not
 * running fails instantly and repeatedly, and lands on "down" after the same
 * budget — a little later than it used to, in exchange for not lying to the
 * one visitor whose opinion matters.
 */

const RETRY_BUDGET_MS = 75_000;
const FIRST_RETRY_MS = 1_500;
const MAX_RETRY_MS = 5_000;

export function useApiHealth(): ApiHealth {
  const [health, setHealth] = useState<ApiHealth>({ state: "checking" });
  const startedAt = useRef(Date.now());

  useEffect(() => {
    const controller = new AbortController();
    let timer: number | undefined;
    let attempt = 0;
    let stopped = false;

    const elapsed = () => Date.now() - startedAt.current;

    const attemptOnce = () => {
      fetch(apiUrl("/health"), { signal: controller.signal })
        .then(async (response) => {
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          const body = (await response.json()) as { version: string };
          if (!stopped) setHealth({ state: "up", version: body.version });
        })
        .catch((error: unknown) => {
          if (stopped || controller.signal.aborted) return;

          if (elapsed() >= RETRY_BUDGET_MS) {
            setHealth({
              state: "down",
              reason: error instanceof Error ? error.message : "unreachable",
            });
            return;
          }

          setHealth({ state: "waking", seconds: Math.round(elapsed() / 1000) });
          attempt += 1;
          // Backs off so a genuinely dead API is not hammered for 75 seconds,
          // but stays frequent early because a waking container can answer at
          // any moment and the wait should end the instant it does.
          const delay = Math.min(FIRST_RETRY_MS * attempt, MAX_RETRY_MS);
          timer = window.setTimeout(attemptOnce, delay);
        });
    };

    attemptOnce();

    return () => {
      stopped = true;
      controller.abort();
      if (timer) window.clearTimeout(timer);
    };
  }, []);

  return health;
}
