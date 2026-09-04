import type { Endpoint } from "./endpoints";

/**
 * The trip in the address bar, so a plan can be sent to somebody.
 *
 * An endpoint round-trips as `agency:stop_id` for a stop, or `lat,lon,label`
 * for a place — the same two forms the API accepts, plus the label, which the
 * API never sees. It only knows a place by its coordinates, so without carrying
 * the name a shared link would come back reading "42.26583, -83.74783" instead
 * of "Michigan Stadium".
 *
 * Written with replaceState rather than pushState: every keystroke that
 * completes a plan would otherwise be a history entry, and Back would walk the
 * rider through their own typing instead of leaving the page.
 */

const FROM = "from";
const TO = "to";
const DEPART = "depart";

export function encodeEndpoint(endpoint: Endpoint): string {
  if (endpoint.id) return endpoint.id;
  return `${endpoint.lat.toFixed(5)},${endpoint.lon.toFixed(5)},${endpoint.label}`;
}

export function decodeEndpoint(raw: string): Endpoint | null {
  const value = raw.trim();
  if (!value) return null;

  if (value.includes(",")) {
    const [lat, lon, ...rest] = value.split(",");
    const latitude = Number(lat);
    const longitude = Number(lon);
    if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) return null;
    const label = rest.join(",").trim();
    return {
      kind: "place",
      label: label || `${latitude.toFixed(5)}, ${longitude.toFixed(5)}`,
      lat: latitude,
      lon: longitude,
    };
  }

  if (!value.includes(":")) return null;
  // A stop's coordinates are not in the link; the label and position are filled
  // in from the plan response, which names both ends anyway.
  return { kind: "stop", label: value, id: value, lat: 0, lon: 0 };
}

export interface TripParams {
  origin: Endpoint | null;
  destination: Endpoint | null;
  depart: string | null;
}

export function readTripFromUrl(search = window.location.search): TripParams {
  const params = new URLSearchParams(search);
  const from = params.get(FROM);
  const to = params.get(TO);
  return {
    origin: from ? decodeEndpoint(from) : null,
    destination: to ? decodeEndpoint(to) : null,
    depart: params.get(DEPART),
  };
}

export function writeTripToUrl(trip: TripParams): void {
  const params = new URLSearchParams();
  if (trip.origin) params.set(FROM, encodeEndpoint(trip.origin));
  if (trip.destination) params.set(TO, encodeEndpoint(trip.destination));
  if (trip.depart) params.set(DEPART, trip.depart);

  const query = params.toString();
  const next = query ? `${window.location.pathname}?${query}` : window.location.pathname;
  if (next !== window.location.pathname + window.location.search) {
    window.history.replaceState(null, "", next);
  }
}
