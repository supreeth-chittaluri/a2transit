// Blank base URL in development: requests go to /api on the Vite dev server,
// which proxies to the backend. In production this is the deployed API origin.
export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "";

export const apiUrl = (path: string): string =>
  API_BASE_URL ? `${API_BASE_URL}${path}` : `/api${path}`;

// Same origin as the API, with the scheme swapped. Deriving it from
// location rather than hardcoding means it works behind TLS in production
// without a second environment variable to forget to set.
export const wsUrl = (path: string): string => {
  const base = API_BASE_URL || `${window.location.origin}/api`;
  return base.replace(/^http/, "ws") + path;
};

// OpenFreeMap serves this style and its tiles free with no API key and no
// account, which is why it is here rather than Mapbox.
export const MAP_STYLE_URL = "https://tiles.openfreemap.org/styles/liberty";

// Downtown Ann Arbor, roughly the Blake Transit Center.
export const ANN_ARBOR_CENTER: [number, number] = [-83.7466, 42.2799];
export const DEFAULT_ZOOM = 12.5;

// TheRide's data licence requires this be displayed wherever their data is
// shown. See docs/feeds.md.
export const ATTRIBUTION =
  "Transit scheduling, geographic, and real-time data provided by permission of AAATA/TheRide. " +
  "Campus transit data from University of Michigan Transit Services.";
