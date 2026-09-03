import { useEffect, useState } from "react";

import { apiUrl } from "./config";

export type ApiHealth =
  | { state: "checking" }
  | { state: "up"; version: string }
  | { state: "down"; reason: string };

/** Polls the backend once on mount so the shell can show whether the API is reachable. */
export function useApiHealth(): ApiHealth {
  const [health, setHealth] = useState<ApiHealth>({ state: "checking" });

  useEffect(() => {
    const controller = new AbortController();

    fetch(apiUrl("/health"), { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const body = (await response.json()) as { version: string };
        setHealth({ state: "up", version: body.version });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setHealth({ state: "down", reason: error instanceof Error ? error.message : "unreachable" });
      });

    return () => controller.abort();
  }, []);

  return health;
}
