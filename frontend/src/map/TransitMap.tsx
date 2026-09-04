import maplibregl from "maplibre-gl";
import { useEffect, useRef } from "react";

import { ANN_ARBOR_CENTER, DEFAULT_ZOOM, MAP_STYLE_URL } from "../config";
import type { Itinerary } from "../lib/api";
import { AGENCY_LABEL, isAgency, legColor } from "../lib/agency";
import type { Vehicle } from "../lib/useVehicles";

import "maplibre-gl/dist/maplibre-gl.css";

interface Props {
  itinerary: Itinerary | null;
  vehicles: Vehicle[];
  onPick?: (lat: number, lon: number) => void;
  /** Insets so fitBounds does not centre the route under the panel. */
  padLeft: number;
  padBottom: number;
}

const ROUTE_SOURCE = "itinerary";
const CASING_LAYER = "itinerary-casing";
const WALK_LAYER = "itinerary-walks";
const RIDE_LAYER = "itinerary-rides";

const reducedMotion = () =>
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/**
 * Ride legs are drawn from the shape the agency publishes, clipped to the
 * stretch actually ridden; walks are a straight dashed line between their ends,
 * which is honest — the router does not know the pavements either.
 */
function toGeoJson(itinerary: Itinerary): GeoJSON.FeatureCollection {
  return {
    type: "FeatureCollection",
    features: itinerary.legs.map((leg, index) => ({
      type: "Feature" as const,
      id: index,
      properties: {
        kind: leg.kind,
        color: leg.kind === "ride" ? legColor(leg.routeColor, leg.agency) : "#8b93a5",
      },
      geometry: {
        type: "LineString" as const,
        // A ride whose trip ships no shape still gets a line, just a straight
        // one. A leg missing from the map reads as a bug in the itinerary.
        coordinates:
          leg.geometry && leg.geometry.length > 1
            ? leg.geometry
            : [
                [leg.fromStop.lon, leg.fromStop.lat],
                [leg.toStop.lon, leg.toStop.lat],
              ],
      },
    })),
  };
}

/**
 * Adds the itinerary source and its three layers, if they are not already
 * there. Idempotent and callable at any time, rather than only from inside the
 * one `load` event — a style can finish loading, or be swapped, at moments a
 * single one-shot listener does not cover.
 */
function addItineraryLayers(instance: maplibregl.Map): boolean {
  if (!instance.isStyleLoaded()) return false;
  if (instance.getSource(ROUTE_SOURCE)) return true;

  instance.addSource(ROUTE_SOURCE, {
    type: "geojson",
    data: { type: "FeatureCollection", features: [] },
  });
  // A dark casing under the coloured line keeps a pale route legible over pale
  // streets without darkening the route colour itself.
  instance.addLayer({
    id: CASING_LAYER,
    type: "line",
    source: ROUTE_SOURCE,
    filter: ["==", ["get", "kind"], "ride"],
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": "#0c0e12", "line-width": 9, "line-opacity": 0.28 },
  });
  instance.addLayer({
    id: WALK_LAYER,
    type: "line",
    source: ROUTE_SOURCE,
    filter: ["==", ["get", "kind"], "walk"],
    layout: { "line-cap": "round" },
    paint: {
      "line-color": ["get", "color"],
      "line-width": 3.5,
      "line-dasharray": [0.6, 1.6],
    },
  });
  instance.addLayer({
    id: RIDE_LAYER,
    type: "line",
    source: ROUTE_SOURCE,
    filter: ["==", ["get", "kind"], "ride"],
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": ["get", "color"], "line-width": 5 },
  });
  return true;
}

export function TransitMap({ itinerary, vehicles, onPick, padLeft, padBottom }: Props) {
  const container = useRef<HTMLDivElement>(null);
  const map = useRef<maplibregl.Map | null>(null);
  const endpointMarkers = useRef<maplibregl.Marker[]>([]);
  const vehicleMarkers = useRef<Map<string, maplibregl.Marker>>(new Map());
  const pick = useRef(onPick);
  pick.current = onPick;

  useEffect(() => {
    if (!container.current || map.current) return;

    const instance = new maplibregl.Map({
      container: container.current,
      style: MAP_STYLE_URL,
      center: ANN_ARBOR_CENTER,
      zoom: DEFAULT_ZOOM,
      attributionControl: { compact: true },
    });
    instance.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    instance.addControl(new maplibregl.GeolocateControl({ trackUserLocation: false }), "top-right");
    map.current = instance;

    instance.on("load", () => addItineraryLayers(instance));

    instance.on("click", (event) => {
      pick.current?.(event.lngLat.lat, event.lngLat.lng);
    });

    if (import.meta.env.DEV) {
      (window as unknown as { __map?: maplibregl.Map }).__map = instance;
    }

    // MapLibre measures its container once at construction and afterwards only
    // listens for window resizes. This map fills a cell that settles after
    // first paint, so without this the canvas keeps its construction-time size.
    const observer = new ResizeObserver(() => instance.resize());
    observer.observe(container.current);

    return () => {
      observer.disconnect();
      instance.remove();
      map.current = null;
    };
  }, []);

  // ------------------------------------------------------------ itinerary
  useEffect(() => {
    const instance = map.current;
    if (!instance) return;

    const draw = (): boolean => {
      // Creates the layers if the style became ready after mount, so a draw is
      // never lost to a listener that fired at the wrong moment.
      if (!addItineraryLayers(instance)) return false;
      const source = instance.getSource(ROUTE_SOURCE) as maplibregl.GeoJSONSource | undefined;
      if (!source) return false;

      for (const marker of endpointMarkers.current) marker.remove();
      endpointMarkers.current = [];

      if (!itinerary) {
        source.setData({ type: "FeatureCollection", features: [] });
        return true;
      }

      const data = toGeoJson(itinerary);
      source.setData(data);

      const first = itinerary.legs[0];
      const last = itinerary.legs[itinerary.legs.length - 1];
      if (first && last) {
        for (const [stop, className, role] of [
          [first.fromStop, "pin pin--origin", "Start"],
          [last.toStop, "pin pin--destination", "Destination"],
        ] as const) {
          const element = document.createElement("div");
          element.className = className;
          endpointMarkers.current.push(
            new maplibregl.Marker({ element })
              .setLngLat([stop.lon, stop.lat])
              .setPopup(
                new maplibregl.Popup({ offset: 14, closeButton: false }).setHTML(
                  `<span class="popup__title">${escapeHtml(stop.name)}</span>` +
                    `<span class="popup__meta">${role}</span>`,
                ),
              )
              .addTo(instance),
          );
        }
      }

      const bounds = new maplibregl.LngLatBounds();
      for (const feature of data.features) {
        for (const position of (feature.geometry as GeoJSON.LineString).coordinates) {
          bounds.extend(position as [number, number]);
        }
      }
      if (!bounds.isEmpty()) {
        // The panel covers part of the viewport — the left on desktop, the
        // bottom on a phone — so an un-inset fit centres the route underneath
        // it and the rider sees an empty map with their journey behind the
        // sheet.
        instance.fitBounds(bounds, {
          padding: {
            top: 90,
            bottom: padBottom + 40,
            left: padLeft + 40,
            right: 60,
          },
          maxZoom: 15,
          duration: reducedMotion() ? 0 : 600,
        });
      }
      return true;
    };

    if (draw()) return;

    // The style was not ready. Retry on every style event until it works, then
    // unsubscribe — rather than a one-shot `once("load")` or `once("idle")`.
    //
    // This is what the deployed site actually needed, and what two previous
    // attempts got wrong: a one-shot listener registered *after* its event has
    // already fired never runs. The route silently never drew while the vehicle
    // markers, which are plain DOM and need no style, carried on working — so
    // the map looked alive and the journey was simply missing from it.
    const retry = () => {
      if (draw()) {
        instance.off("styledata", retry);
        instance.off("idle", retry);
      }
    };
    instance.on("styledata", retry);
    instance.on("idle", retry);
    return () => {
      instance.off("styledata", retry);
      instance.off("idle", retry);
    };
  }, [itinerary, padLeft, padBottom]);

  // -------------------------------------------------------------- vehicles
  useEffect(() => {
    const instance = map.current;
    if (!instance) return;

    const seen = new Set<string>();
    for (const vehicle of vehicles) {
      const key = `${vehicle.agency}:${vehicle.vehicleId}`;
      seen.add(key);
      let marker = vehicleMarkers.current.get(key);

      if (!marker) {
        const element = document.createElement("div");
        element.className = `vehicle vehicle--${isAgency(vehicle.agency) ? vehicle.agency : "theride"}`;
        element.innerHTML =
          '<span class="vehicle__heading"></span><span class="vehicle__body"></span>';
        marker = new maplibregl.Marker({ element, rotationAlignment: "map" })
          .setLngLat([vehicle.lon, vehicle.lat])
          .addTo(instance);
        vehicleMarkers.current.set(key, marker);
      } else {
        // Moved rather than recreated, so the CSS transition animates the bus
        // between poll cycles instead of it teleporting every twenty seconds.
        marker.setLngLat([vehicle.lon, vehicle.lat]);
      }

      marker.setRotation(vehicle.bearing ?? 0);
      // Not every vehicle is on a trip — a bus deadheading reports a position
      // and no route, and the popup should say so rather than show a blank.
      const agency = isAgency(vehicle.agency) ? vehicle.agency : "theride";
      marker.setPopup(
        new maplibregl.Popup({ offset: 12, closeButton: false }).setHTML(
          `<span class="popup__title">${
            vehicle.routeId ? `Route ${escapeHtml(vehicle.routeId)}` : "Not in service"
          }</span>` + `<span class="popup__meta">${AGENCY_LABEL[agency]} · bus ${escapeHtml(vehicle.vehicleId)}</span>`,
        ),
      );
    }

    for (const [key, marker] of vehicleMarkers.current) {
      if (!seen.has(key)) {
        marker.remove();
        vehicleMarkers.current.delete(key);
      }
    }
  }, [vehicles]);

  return <div ref={container} className="map-canvas" />;
}

/** Popups take HTML so the agency name can be styled; stop names are feed data. */
function escapeHtml(value: string): string {
  return value.replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c] ?? c,
  );
}
