import { useEffect, useRef, useState } from "react";

import { wsUrl } from "../config";

export interface Vehicle {
  agency: string;
  vehicleId: string;
  tripId: string | null;
  routeId: string | null;
  lat: number;
  lon: number;
  bearing: number | null;
  speedMps: number | null;
  timestamp: number;
}

export type VehicleFeed =
  | { state: "connecting"; vehicles: Vehicle[] }
  | { state: "live"; vehicles: Vehicle[] }
  | { state: "offline"; vehicles: Vehicle[] };

/** Longest wait between reconnection attempts, in ms. */
const MAX_BACKOFF_MS = 15_000;

/**
 * Live vehicle positions over the WebSocket the poller feeds.
 *
 * Every message is a complete snapshot rather than a diff, which makes the
 * reconnect story trivial: drop the socket, open a new one, and the first frame
 * is the whole truth again. There is no state to reconcile and no chance of a
 * missed update leaving a ghost bus on the map forever.
 *
 * Reconnection backs off, because the common reason the socket closes is that
 * the API is restarting and hammering it does not help. `offline` is a normal
 * state, not an error: realtime is an enhancement over a planner that works
 * perfectly well from the schedule.
 */
export function useVehicles(enabled = true): VehicleFeed {
  const [feed, setFeed] = useState<VehicleFeed>({ state: "connecting", vehicles: [] });
  const socket = useRef<WebSocket | null>(null);

  useEffect(() => {
    if (!enabled) return;

    let closed = false;
    let attempt = 0;
    let timer: number | undefined;

    const connect = () => {
      if (closed) return;
      const ws = new WebSocket(wsUrl("/ws/vehicles"));
      socket.current = ws;

      ws.onopen = () => {
        attempt = 0;
      };

      ws.onmessage = (event) => {
        try {
          const body = JSON.parse(event.data as string) as { vehicles?: Vehicle[] };
          if (Array.isArray(body.vehicles)) {
            setFeed({ state: "live", vehicles: body.vehicles });
          }
        } catch {
          /* a frame we cannot read is not a reason to tear the socket down */
        }
      };

      ws.onerror = () => ws.close();

      ws.onclose = () => {
        if (closed) return;
        // Keep the last known positions on screen while reconnecting. They are
        // seconds old, which is better than an empty map.
        setFeed((current) => ({ state: "offline", vehicles: current.vehicles }));
        attempt += 1;
        timer = window.setTimeout(connect, Math.min(2 ** attempt * 500, MAX_BACKOFF_MS));
      };
    };

    connect();

    return () => {
      closed = true;
      window.clearTimeout(timer);
      socket.current?.close();
      socket.current = null;
    };
  }, [enabled]);

  return feed;
}
