/**
 * Agency identity in one place.
 *
 * TheRide's red and Michigan's navy appear on legs, route lines, map markers
 * and badges. A rider learns "red is the city bus" once, and that only holds if
 * every surface reads the colour from here — the same reason `legColor` refuses
 * to fall back to a neutral grey.
 */

export type Agency = "theride" | "mbus";

export const AGENCY_LABEL: Record<Agency, string> = {
  theride: "TheRide",
  mbus: "MBus",
};

/**
 * Light-mode brand values, duplicated from tokens.css.
 *
 * Deliberate: these are the answer when there is no stylesheet to read — a
 * test environment, or a call before first paint — and returning a grey there
 * would silently drop the agency colouring the whole design leans on.
 */
export const AGENCY_FALLBACK: Record<Agency, string> = {
  theride: "#c8102e",
  mbus: "#00274c",
};

export const isAgency = (value: string | null | undefined): value is Agency =>
  value === "theride" || value === "mbus";

/** The live token value where a stylesheet exists, so dark mode lifts it. */
export function agencyColor(agency: string | null | undefined): string {
  const key: Agency = isAgency(agency) ? agency : "theride";
  if (typeof window === "undefined" || typeof getComputedStyle !== "function") {
    return AGENCY_FALLBACK[key];
  }
  const value = getComputedStyle(document.documentElement)
    .getPropertyValue(key === "mbus" ? "--mbus" : "--theride")
    .trim();
  return value || AGENCY_FALLBACK[key];
}

/**
 * A leg's colour. The feed's own route colour wins where it has one — a rider
 * matching the app against a printed timetable should see the same colour —
 * otherwise the agency colour.
 */
export function legColor(
  routeColor: string | null | undefined,
  agency: string | null | undefined,
): string {
  if (routeColor) return routeColor.startsWith("#") ? routeColor : `#${routeColor}`;
  return agencyColor(agency);
}
