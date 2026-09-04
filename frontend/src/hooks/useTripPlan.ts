import { useEffect, useState } from "react";

import { ApiError, planTrip, type PlanResponse } from "../lib/api";

export type PlanState =
  | { state: "idle" }
  | { state: "loading" }
  | { state: "done"; response: PlanResponse }
  | { state: "error"; message: string };

/**
 * The planning request, as a state machine.
 *
 * Lifted out of App so the component describes layout rather than fetch
 * bookkeeping. Every change of origin, destination or time aborts the request
 * in flight — otherwise a slow earlier answer can land after a fast later one
 * and the rider sees a plan for a trip they have already changed.
 */
export function useTripPlan(
  from: string | null,
  to: string | null,
  depart: string,
): PlanState {
  const [state, setState] = useState<PlanState>({ state: "idle" });

  useEffect(() => {
    if (!from || !to) {
      setState({ state: "idle" });
      return;
    }

    const controller = new AbortController();
    setState({ state: "loading" });

    planTrip(from, to, depart, controller.signal)
      .then((response) => setState({ state: "done", response }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setState({
          state: "error",
          message:
            error instanceof ApiError
              ? error.message
              : "Could not reach the planner — it may still be waking up. " +
                "The free tier sleeps when idle; this usually clears in under a minute.",
        });
      });

    return () => controller.abort();
  }, [from, to, depart]);

  return state;
}
