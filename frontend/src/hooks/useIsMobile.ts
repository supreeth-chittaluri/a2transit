import { useEffect, useState } from "react";

/** Matches the 860px breakpoint in layout.css, where the rail becomes a sheet. */
const QUERY = "(max-width: 860px)";

export function useIsMobile(): boolean {
  const [isMobile, setIsMobile] = useState(
    () => typeof window !== "undefined" && window.matchMedia(QUERY).matches,
  );

  useEffect(() => {
    const media = window.matchMedia(QUERY);
    const onChange = () => setIsMobile(media.matches);
    media.addEventListener("change", onChange);
    // Rotating a phone crosses the breakpoint, and so does a desktop window
    // being dragged narrow — both must re-render the panel, not just restyle it,
    // because the sheet has drag state the rail does not.
    onChange();
    return () => media.removeEventListener("change", onChange);
  }, []);

  return isMobile;
}
