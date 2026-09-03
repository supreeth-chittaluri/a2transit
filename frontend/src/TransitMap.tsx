import maplibregl from "maplibre-gl";
import { useEffect, useRef } from "react";

import { ANN_ARBOR_CENTER, DEFAULT_ZOOM, MAP_STYLE_URL } from "./config";

import "maplibre-gl/dist/maplibre-gl.css";

/**
 * The base map. Itinerary geometry and live vehicle markers get layered on in
 * M6 and M7; for now this exists to prove tiles render with no key and no bill.
 */
export function TransitMap() {
  const container = useRef<HTMLDivElement>(null);
  const map = useRef<maplibregl.Map | null>(null);

  useEffect(() => {
    if (!container.current || map.current) return;

    const instance = new maplibregl.Map({
      container: container.current,
      style: MAP_STYLE_URL,
      center: ANN_ARBOR_CENTER,
      zoom: DEFAULT_ZOOM,
    });
    instance.addControl(new maplibregl.NavigationControl(), "top-right");
    map.current = instance;

    // Dev-only handle so the map can be poked from the browser console.
    if (import.meta.env.DEV) {
      (window as unknown as { __map?: maplibregl.Map }).__map = instance;
    }

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
    };
  }, []);

  return <div ref={container} className="map" />;
}
