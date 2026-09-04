import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";

import { useIsMobile } from "../hooks/useIsMobile";

/** Fractions of the viewport the sheet snaps to. */
const SNAPS = [0.18, 0.55, 0.92] as const;
const DEFAULT_SNAP = 1;

interface Props {
  children: ReactNode;
  /** Nudges the sheet open when results arrive, if the rider left it shut. */
  expandSignal?: number;
}

/**
 * The panel: a fixed rail on desktop, a draggable sheet on a phone.
 *
 * On a phone the map and the itinerary want the same space and only the rider
 * knows which they need, so the sheet drags between three snap points rather
 * than picking one for them. Height is written to a CSS variable and the
 * transition is switched off mid-drag, so it tracks the finger exactly and
 * animates only when it snaps.
 *
 * Pointer events rather than touch events: the same handler then covers a
 * finger, a trackpad drag and a mouse, and `setPointerCapture` keeps the drag
 * alive when the finger leaves the 26px handle, which it always does.
 */
export function Panel({ children, expandSignal = 0 }: Props) {
  const isMobile = useIsMobile();
  const [snap, setSnap] = useState(DEFAULT_SNAP);
  const [dragHeight, setDragHeight] = useState<number | null>(null);
  const sheet = useRef<HTMLElement>(null);
  const drag = useRef<{ startY: number; startHeight: number } | null>(null);

  const heightFor = (index: number) => `${SNAPS[index] * 100}dvh`;

  // Results arriving while the sheet is at its lowest snap would be invisible;
  // lifting it is what a rider would do next anyway. Never lowers it — that
  // would fight somebody who deliberately pushed it down to see the map.
  useEffect(() => {
    if (!expandSignal || !isMobile) return;
    setSnap((current) => (current === 0 ? 1 : current));
  }, [expandSignal, isMobile]);

  const onPointerDown = useCallback((event: React.PointerEvent<HTMLButtonElement>) => {
    const element = sheet.current;
    if (!element) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    drag.current = { startY: event.clientY, startHeight: element.getBoundingClientRect().height };
    setDragHeight(element.getBoundingClientRect().height);
  }, []);

  const onPointerMove = useCallback((event: React.PointerEvent<HTMLButtonElement>) => {
    if (!drag.current) return;
    // Dragging up grows the sheet, so the delta is inverted.
    const next = drag.current.startHeight - (event.clientY - drag.current.startY);
    const max = window.innerHeight * SNAPS[SNAPS.length - 1];
    const min = window.innerHeight * SNAPS[0] * 0.6;
    setDragHeight(Math.min(Math.max(next, min), max));
  }, []);

  const endDrag = useCallback(() => {
    if (!drag.current) return;
    const height = dragHeight ?? drag.current.startHeight;
    drag.current = null;
    // Snap to whichever point the sheet was released nearest.
    const ratio = height / window.innerHeight;
    let nearest = 0;
    for (let i = 1; i < SNAPS.length; i += 1) {
      if (Math.abs(SNAPS[i] - ratio) < Math.abs(SNAPS[nearest] - ratio)) nearest = i;
    }
    setSnap(nearest);
    setDragHeight(null);
  }, [dragHeight]);

  // The handle is a real button, so the sheet is operable without a pointer.
  const onKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === "ArrowUp" || event.key === "ArrowRight") {
      event.preventDefault();
      setSnap((s) => Math.min(s + 1, SNAPS.length - 1));
    } else if (event.key === "ArrowDown" || event.key === "ArrowLeft") {
      event.preventDefault();
      setSnap((s) => Math.max(s - 1, 0));
    }
  };

  const style = isMobile
    ? ({ "--sheet-height": dragHeight ? `${dragHeight}px` : heightFor(snap) } as React.CSSProperties)
    : undefined;

  return (
    <aside
      ref={sheet}
      className={`panel${dragHeight !== null ? " panel--dragging" : ""}`}
      style={style}
      aria-label="Trip planner"
    >
      <button
        type="button"
        className="panel__grabber"
        aria-label={`Resize panel, currently ${["collapsed", "half", "expanded"][snap]}`}
        aria-valuenow={snap}
        aria-valuemin={0}
        aria-valuemax={SNAPS.length - 1}
        role="slider"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        onKeyDown={onKeyDown}
      />
      {children}
    </aside>
  );
}
