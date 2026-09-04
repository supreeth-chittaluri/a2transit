import type React from "react";
import { useEffect, useId, useRef, useState } from "react";

import { ApiError, geocode, searchStops } from "../lib/api";
import type { Endpoint } from "../lib/endpoints";

interface Props {
  label: string;
  placeholder: string;
  kind: "origin" | "destination";
  value: Endpoint | null;
  onChange: (endpoint: Endpoint | null) => void;
  /** Only one field's list may be open; the parent owns which. */
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
 * Three details exist because the two lookups finish at different times, and
 * all three were bugs first:
 *
 *   * Only one field's list is open at a time. They are absolutely positioned
 *     at the same depth, so two open lists meant the upper one covered the
 *     lower one's options.
 *   * The address goes at the bottom. Inserting it at the top when it arrived
 *     pushed every stop down a row just as someone was about to click one.
 *   * The list closes on a click outside it, never on blur and never on
 *     pointerdown — both land before the row's own handler and unmount the
 *     button mid-press, so the list visibly reacted and nothing got chosen.
 *
 * Keyboard: arrows move the highlight, Enter takes it, Escape closes. Hand-
 * rolled rather than a `<datalist>` because a stop's routes have to appear on a
 * second line — they are what tell three identically-named stops apart — and
 * the ARIA combobox roles are what make that legible to a screen reader.
 */
export function EndpointField({
  label,
  placeholder,
  kind,
  value,
  onChange,
  isOpen,
  onOpenChange,
}: Props) {
  const [query, setQuery] = useState("");
  const [suggestions, setSuggestions] = useState<Suggestions>(EMPTY);
  const [loading, setLoading] = useState(false);
  const [highlighted, setHighlighted] = useState(-1);
  const fieldId = useId();
  const listId = `${fieldId}-list`;
  const container = useRef<HTMLDivElement>(null);

  // A chosen endpoint owns the field's text until the rider types again.
  useEffect(() => {
    if (value) setQuery(value.label);
  }, [value]);

  useEffect(() => {
    if (!isOpen || query.trim().length < MIN_QUERY || query === value?.label) {
      setSuggestions(EMPTY);
      setLoading(false);
      return;
    }

    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      setLoading(true);
      const text = query.trim();
      let settled = 0;
      const done = () => {
        settled += 1;
        if (settled === 2 && !controller.signal.aborted) setLoading(false);
      };

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
        })
        .finally(done);

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
        .finally(done);
    }, DEBOUNCE_MS);

    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [query, isOpen, value?.label]);

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
    setHighlighted(-1);
  };

  const options: Endpoint[] = [
    ...suggestions.stops,
    ...(suggestions.address ? [suggestions.address] : []),
  ];
  const hasSuggestions = options.length > 0;

  // A highlight left pointing at row 7 of a list that now has two would be
  // invisible, and Enter would choose nothing.
  useEffect(() => {
    setHighlighted((current) => (current >= options.length ? -1 : current));
  }, [options.length]);

  const onKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Escape") {
      onOpenChange(false);
      setHighlighted(-1);
      return;
    }
    if (!isOpen || !hasSuggestions) {
      if (event.key === "ArrowDown") onOpenChange(true);
      return;
    }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const step = event.key === "ArrowDown" ? 1 : -1;
      setHighlighted((current) => {
        const next = current + step;
        // Wrapping rather than clamping: at the end of a short list, one more
        // Down much more likely means "start again" than "do nothing".
        if (next < 0) return options.length - 1;
        if (next >= options.length) return 0;
        return next;
      });
      return;
    }
    if (event.key === "Enter" && highlighted >= 0) {
      event.preventDefault();
      choose(options[highlighted]);
    }
  };

  const showList = isOpen && query.trim().length >= MIN_QUERY;

  return (
    <div className={`field${value ? " field--chosen" : ""}`} ref={container}>
      <label className="field__label" htmlFor={fieldId}>
        {label}
      </label>

      <div className="field__control">
        <span className={`field__marker field__marker--${kind}`} aria-hidden />
        <input
          id={fieldId}
          className="field__input"
          type="text"
          autoComplete="off"
          role="combobox"
          aria-expanded={showList && hasSuggestions}
          aria-controls={listId}
          aria-autocomplete="list"
          aria-activedescendant={
            highlighted >= 0 ? `${listId}-option-${highlighted}` : undefined
          }
          placeholder={placeholder}
          value={query}
          onKeyDown={onKeyDown}
          onChange={(event) => {
            setQuery(event.target.value);
            onOpenChange(true);
            setHighlighted(-1);
            // The old choice is stale the moment the text stops matching it.
            if (value) onChange(null);
          }}
          onFocus={() => onOpenChange(true)}
        />
        <span className="field__affordance">
          {loading && <span className="spinner" aria-hidden />}
          {!loading && query && (
            <button
              type="button"
              className="field__clear"
              aria-label={`Clear ${label.toLowerCase()}`}
              onClick={() => {
                setQuery("");
                onChange(null);
                onOpenChange(false);
              }}
            >
              ✕
            </button>
          )}
        </span>
      </div>

      {showList && (
        <ul className="suggestions scroll-y" id={listId} role="listbox" aria-label={`${label} results`}>
          {options.map((option, index) => (
            <li key={option.id ?? `place-${index}`} role="presentation">
              <button
                type="button"
                id={`${listId}-option-${index}`}
                role="option"
                aria-selected={index === highlighted}
                className={`suggestion${index === highlighted ? " suggestion--active" : ""}`}
                onMouseEnter={() => setHighlighted(index)}
                onClick={() => choose(option)}
              >
                <span className="suggestion__icon" aria-hidden>
                  {option.kind === "place" ? "⌖" : "⬤"}
                </span>
                <span className="suggestion__text">
                  <span className="suggestion__name">{option.label}</span>
                  <span className="suggestion__meta">
                    {option.kind === "place"
                      ? "Address"
                      : option.routes?.length
                        ? option.routes.slice(0, 5).join(" · ")
                        : option.id?.split(":")[0]}
                  </span>
                </span>
              </button>
            </li>
          ))}

          {!hasSuggestions && (
            <li className="suggestions__empty" role="presentation">
              {loading ? (
                <>
                  <span className="spinner" aria-hidden />
                  Searching…
                </>
              ) : (
                (suggestions.error ?? "Nothing found")
              )}
            </li>
          )}
        </ul>
      )}
    </div>
  );
}
