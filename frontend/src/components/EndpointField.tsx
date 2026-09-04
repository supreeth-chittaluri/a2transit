import { useEffect, useId, useRef, useState } from "react";

import { ApiError, geocode, searchStops } from "../lib/api";
import type { Endpoint } from "../lib/endpoints";

interface Props {
  label: string;
  placeholder: string;
  value: Endpoint | null;
  onChange: (endpoint: Endpoint | null) => void;
  /** Whether this field's suggestion list is the one showing. */
  isOpen: boolean;
  onOpenChange: (open: boolean) => void;
}

interface Suggestions {
  stops: Endpoint[];
  address: Endpoint | null;
  error: string | null;
}

const EMPTY: Suggestions = { stops: [], address: null, error: null };
const DEBOUNCE_MS = 200;
const MIN_QUERY = 2;

/**
 * One field that accepts either a stop or an address.
 *
 * Both lookups fire together rather than making the rider choose a mode first.
 * They are different services — a trigram query against our own stops, and a
 * third-party geocoder — but "Kerrytown" is a plausible answer from either, and
 * asking someone to declare which kind of thing they are about to type is the
 * sort of interface that only makes sense to whoever built it.
 *
 * The geocoder is the slower and more rate-limited of the two, so stops render
 * as soon as they arrive rather than waiting for the address.
 *
 * Which field is showing its list is the parent's state, not this component's.
 * Two lists are absolutely positioned at the same depth, so if both were open
 * the upper one would cover the lower one's options — clicks landing on a list
 * belonging to a field the rider is not using.
 *
 * Two details exist because the two lookups finish at different times:
 *
 *   * The address goes at the *bottom*. Inserting it at the top when it arrives
 *     pushes every stop down by a row, and a rider who was about to click the
 *     second stop clicks the third instead.
 *   * The list closes on a click outside it, never on blur and never on
 *     pointerdown. Both of those land before the row's own handler and unmount
 *     the button mid-press, so the rider sees the list react and nothing get
 *     chosen.
 */
export function EndpointField({
  label,
  placeholder,
  value,
  onChange,
  isOpen,
  onOpenChange,
}: Props) {
  const [query, setQuery] = useState("");
  const [suggestions, setSuggestions] = useState<Suggestions>(EMPTY);
  const [loading, setLoading] = useState(false);
  const listId = useId();
  const container = useRef<HTMLDivElement>(null);

  // A chosen endpoint owns the field's text until the rider types again.
  useEffect(() => {
    if (value) setQuery(value.label);
  }, [value]);

  useEffect(() => {
    if (!isOpen || query.trim().length < MIN_QUERY || query === value?.label) {
      setSuggestions(EMPTY);
      return;
    }

    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      setLoading(true);
      const text = query.trim();

      searchStops(text, controller.signal)
        .then((results) =>
          setSuggestions((current) => ({
            ...current,
            error: null,
            stops: results.map((stop) => ({
              kind: "stop" as const,
              label: stop.name,
              id: stop.id,
              lat: stop.lat,
              lon: stop.lon,
              routes: stop.routes,
            })),
          })),
        )
        .catch((error: unknown) => {
          if (controller.signal.aborted) return;
          setSuggestions((current) => ({
            ...current,
            error: error instanceof ApiError ? error.message : "Stop search failed",
          }));
        });

      geocode(text, controller.signal)
        .then((result) =>
          setSuggestions((current) => ({
            ...current,
            address: {
              kind: "place" as const,
              label: result.name,
              lat: result.lat,
              lon: result.lon,
            },
          })),
        )
        // A geocoder finding nothing is the ordinary case for a stop name, not
        // an error worth showing.
        .catch(() => setSuggestions((current) => ({ ...current, address: null })))
        .finally(() => {
          if (!controller.signal.aborted) setLoading(false);
        });
    }, DEBOUNCE_MS);

    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [query, isOpen, value?.label]);

  // Clicking the map, or anywhere outside, should close the list.
  //
  // On click rather than pointerdown. Pointerdown fires *before* the row's own
  // handler, so closing there unmounts the button the rider is in the middle of
  // pressing and the click never arrives — the list visibly reacts and nothing
  // gets selected.
  useEffect(() => {
    if (!isOpen) return;
    const onDocumentClick = (event: MouseEvent) => {
      if (!container.current?.contains(event.target as Node)) onOpenChange(false);
    };
    document.addEventListener("click", onDocumentClick);
    return () => document.removeEventListener("click", onDocumentClick);
  }, [isOpen, onOpenChange]);

  const choose = (endpoint: Endpoint) => {
    onChange(endpoint);
    setQuery(endpoint.label);
    onOpenChange(false);
  };

  const hasSuggestions = suggestions.stops.length > 0 || suggestions.address !== null;

  return (
    <div className="field" ref={container}>
      <label className="field__label" htmlFor={listId}>
        {label}
      </label>
      <div className="field__control">
        <input
          id={listId}
          className="field__input"
          type="text"
          autoComplete="off"
          placeholder={placeholder}
          value={query}
          onChange={(event) => {
            setQuery(event.target.value);
            onOpenChange(true);
            // The old choice is stale the moment the text stops matching it.
            if (value) onChange(null);
          }}
          onFocus={() => onOpenChange(true)}
        />
        {query && (
          <button
            className="field__clear"
            type="button"
            aria-label={`Clear ${label.toLowerCase()}`}
            onClick={() => {
              setQuery("");
              onChange(null);
              onOpenChange(false);
            }}
          >
            ×
          </button>
        )}
      </div>

      {isOpen && query.trim().length >= MIN_QUERY && (
        <ul className="suggestions">
          {suggestions.stops.map((stop) => (
            <li key={stop.id}>
              <button
                type="button"
                className="suggestion"
                onClick={() => choose(stop)}
              >
                <span className="suggestion__name">{stop.label}</span>
                <span className="suggestion__meta">
                  {stop.routes?.length ? stop.routes.slice(0, 4).join(" · ") : stop.id?.split(":")[0]}
                </span>
              </button>
            </li>
          ))}
          {suggestions.address && (
            <li>
              <button
                type="button"
                className="suggestion suggestion--address"
                onClick={() => choose(suggestions.address!)}
              >
                <span className="suggestion__name">{suggestions.address.label}</span>
                <span className="suggestion__meta">address</span>
              </button>
            </li>
          )}
          {!hasSuggestions && (
            <li className="suggestions__empty">
              {loading ? "Searching…" : (suggestions.error ?? "Nothing found")}
            </li>
          )}
        </ul>
      )}
    </div>
  );
}
