/**
 * What the two search fields hold, and how it reaches the API.
 *
 * A stop and a place are different things to a rider — one has a name on a
 * pole, the other is where they are standing — but the API takes either in the
 * same parameter, so the difference lives here and nowhere else.
 */
export interface Endpoint {
  kind: "stop" | "place";
  /** What the field displays. */
  label: string;
  /** `agency:stop_id` for a stop; unset for a place. */
  id?: string;
  lat: number;
  lon: number;
  /** Routes calling at a stop, for telling three identical names apart. */
  routes?: string[];
}

/** The `from`/`to` value the API expects. */
export const toQueryValue = (endpoint: Endpoint): string =>
  endpoint.id ?? `${endpoint.lat},${endpoint.lon}`;

/**
 * A local ISO string with no timezone, which is what the API parses.
 *
 * `toISOString()` would convert to UTC, so a 09:00 departure in Ann Arbor
 * becomes 13:00 and the rider is shown the wrong afternoon's buses.
 */
export function toLocalIso(date: Date): string {
  const pad = (value: number) => String(value).padStart(2, "0");
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}`
  );
}

export const formatClock = (iso: string): string =>
  new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

export function formatDuration(seconds: number): string {
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  return `${hours} h ${String(minutes % 60).padStart(2, "0")}`;
}
