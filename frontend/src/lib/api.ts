/**
 * Typed client for the a2transit API.
 *
 * Field names arrive camelCase (the backend aliases them on the way out), so
 * these interfaces are the wire format rather than a translation of it.
 */
import { apiUrl } from "../config";

export interface StopRef {
  /** `agency:stop_id`, or null for a place that is not a stop. */
  id: string | null;
  name: string;
  lat: number;
  lon: number;
  agency: string | null;
}

export interface Leg {
  kind: "ride" | "walk";
  fromStop: StopRef;
  toStop: StopRef;
  depart: string;
  arrive: string;
  seconds: number;

  agency?: string | null;
  routeId?: string | null;
  routeLabel?: string | null;
  routeColor?: string | null;
  tripId?: string | null;
  headsign?: string | null;
  intermediateStops?: number | null;
  /** [[lon, lat], ...] along the published shape. */
  geometry?: number[][] | null;

  distanceMetres?: number | null;
}

export interface Itinerary {
  departure: string;
  arrival: string;
  durationSeconds: number;
  transfers: number;
  rideCount: number;
  walkSeconds: number;
  legs: Leg[];
}

export interface PlanResponse {
  origin: StopRef;
  destination: StopRef;
  requestedDeparture: string;
  itineraries: Itinerary[];
  engine: string;
  queryMs: number;
  attribution: string;
}

export interface StopSearchResult {
  id: string;
  name: string;
  agency: string;
  lat: number;
  lon: number;
  routes: string[];
}

export interface GeocodeResult {
  query: string;
  name: string;
  lat: number;
  lon: number;
  provider: string;
}

/** An error the user can act on, rather than a stack trace. */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function get<T>(path: string, params: Record<string, string>, signal?: AbortSignal): Promise<T> {
  const query = new URLSearchParams(params).toString();
  const response = await fetch(apiUrl(`${path}?${query}`), { signal });

  if (!response.ok) {
    // FastAPI puts the useful sentence in `detail`; a 422 from validation puts
    // a list of objects there instead, which is not something to show a rider.
    let detail = `Request failed (HTTP ${response.status})`;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      /* the body was not JSON; the generic message stands */
    }
    throw new ApiError(detail, response.status);
  }

  return (await response.json()) as T;
}

export const planTrip = (
  from: string,
  to: string,
  depart: string,
  signal?: AbortSignal,
): Promise<PlanResponse> => get<PlanResponse>("/plan", { from, to, depart }, signal);

export const searchStops = (query: string, signal?: AbortSignal): Promise<StopSearchResult[]> =>
  get<{ results: StopSearchResult[] }>("/stops/search", { q: query }, signal).then(
    (body) => body.results,
  );

export const geocode = (query: string, signal?: AbortSignal): Promise<GeocodeResult> =>
  get<GeocodeResult>("/geocode", { q: query }, signal);
