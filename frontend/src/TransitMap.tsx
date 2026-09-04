import maplibregl from "maplibre-gl";
import { useEffect, useRef } from "react";

import { ANN_ARBOR_CENTER, DEFAULT_ZOOM, MAP_STYLE_URL } from "./config";
import type { Itinerary } from "./lib/api";
import { legColor } from "./lib/endpoints";

import "maplibre-gl/dist/maplibre-gl.css";

interface Props {
  itinerary: Itinerary | null;
  onPick?: (lat: number, lon: number) => void;
}

const ROUTE_SOURCE = "itinerary";
const RIDE_LAYER = "itinerary-rides";
const WALK_LAYER = "itinerary-walks";
const CASING_LAYER = "itinerary-casing";

/**
 * Ride legs are drawn from the shape the agency publishes, clipped to the
 * stretch actually ridden; walk legs are a straight dashed line between their
 * ends, which is honest — the router does not know the pavements either.
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
        label: leg.routeLabel ?? "",
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

function boundsOf(itinerary: Itinerary): maplibregl.LngLatBounds {
  const bounds = new maplibregl.LngLatBounds();
  for (const feature of toGeoJson(itinerary).features) {
    for (const position of (feature.geometry as GeoJSON.LineString).coordinates) {
      bounds.extend(position as [number, number]);
    }
  }
  return bounds;
}

export function TransitMap({ itinerary, onPick }: Props) {
  const container = useRef<HTMLDivElement>(null);
  const map = useRef<maplibregl.Map | null>(null);
  const ready = useRef(false);
  const markers = useRef<maplibregl.Marker[]>([]);
  // Held in a ref so changing the handler does not tear down the map.
  const pick = useRef(onPick);
  pick.current = onPick;

  useEffect(() => {
    if (!container.current || map.current) return;

    const instance = new maplibregl.Map({
      container: container.current,
      style: MAP_STYLE_URL,
      center: ANN_ARBOR_CENTER,
      zoom: DEFAULT_ZOOM,
    });
    instance.addControl(new maplibregl.NavigationControl(), "top-right");
    instance.addControl(
      new maplibregl.GeolocateControl({ trackUserLocation: false }),
      "top-right",
    );
    map.current = instance;

    instance.on("load", () => {
      instance.addSource(ROUTE_SOURCE, {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });
      // A dark casing under the coloured line keeps a pale route legible
      // against pale streets without darkening the route colour itself.
      instance.addLayer({
        id: CASING_LAYER,
        type: "line",
        source: ROUTE_SOURCE,
        filter: ["==", ["get", "kind"], "ride"],
        layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": "#10131a", "line-width": 8, "line-opacity": 0.5 },
      });
      instance.addLayer({
        id: WALK_LAYER,
        type: "line",
        source: ROUTE_SOURCE,
        filter: ["==", ["get", "kind"], "walk"],
        layout: { "line-cap": "round" },
        paint: {
          "line-color": ["get", "color"],
          "line-width": 3,
          "line-dasharray": [1, 2],
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
      ready.current = true;
    });

    instance.on("click", (event) => {
      pick.current?.(event.lngLat.lat, event.lngLat.lng);
    });

    // MapLibre measures its container once at construction and afterwards only
    // listens for window resizes. This map lives in a grid cell that settles
    // after first paint, so without this the canvas keeps whatever size the
    // cell had at construction and the map renders into a sliver.
    const observer = new ResizeObserver(() => instance.resize());
    observer.observe(container.current);

    return () => {
      observer.disconnect();
      instance.remove();
      map.current = null;
      ready.current = false;
    };
  }, []);

  useEffect(() => {
    const instance = map.current;
    if (!instance) return;

    const draw = () => {
      const source = instance.getSource(ROUTE_SOURCE) as maplibregl.GeoJSONSource | undefined;
      if (!source) return;

      for (const marker of markers.current) marker.remove();
      markers.current = [];

      if (!itinerary) {
        source.setData({ type: "FeatureCollection", features: [] });
        return;
      }

      source.setData(toGeoJson(itinerary));

      const first = itinerary.legs[0];
      const last = itinerary.legs[itinerary.legs.length - 1];
      if (first && last) {
        for (const [stop, className] of [
          [first.fromStop, "pin pin--origin"],
          [last.toStop, "pin pin--destination"],
        ] as const) {
          const element = document.createElement("div");
          element.className = className;
          markers.current.push(
            new maplibregl.Marker({ element })
              .setLngLat([stop.lon, stop.lat])
              .setPopup(new maplibregl.Popup({ offset: 12 }).setText(stop.name))
              .addTo(instance),
          );
        }
      }

      const bounds = boundsOf(itinerary);
      if (!bounds.isEmpty()) {
        instance.fitBounds(bounds, { padding: 60, maxZoom: 15, duration: 600 });
      }
    };

    // The itinerary can arrive before the style finishes loading, in which case
    // the source does not exist yet and the draw would be silently dropped.
    if (ready.current) draw();
    else instance.once("load", draw);
  }, [itinerary]);

  return <div ref={container} className="map" />;
}
