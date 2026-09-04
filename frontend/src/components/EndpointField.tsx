import type React from "react";
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
 *
 * Keyboard: arrows move the highlight, Enter takes it, Escape closes without
 * choosing. Wired by hand rather than left to the browser because a `<datalist>`
 * cannot show a second line per row, and a stop's routes are what tell three
 * identically-named ones apart. The ARIA combobox roles are what make that
 * legible to a screen reader — a `<ul>` of buttons announces as a list of
 * buttons, which is true and useless.
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
    setHighlighted(-1);
  };

  // One ordered list, so arrow keys and clicks traverse the same thing. The
  // address is last: it arrives after the stops and inserting it at the top
  // would move every row out from under the cursor.
  const options: Endpoint[] = [
    ...suggestions.stops,
    ...(suggestions.address ? [suggestions.address] : []),
  ];
  const hasSuggestions = options.length > 0;

  // A highlight left pointing at row 7 of a list that now has two would be
  // invisible and would make Enter choose nothing.
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
        // Down is much more likely to mean "start again" than "do nothing".
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

  return (
    <div className="field" ref={container}>
      <label className="field__label" htmlFor={fieldId}>
        {label}
      </label>
      <div className="field__control">
        <input
          id={fieldId}
          className="field__input"
          type="text"
          autoComplete="off"
          role="combobox"
          aria-expanded={isOpen && hasSuggestions}
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
        <ul className="suggestions" id={listId} role="listbox" aria-label={`${label} results`}>
          {options.map((option, index) => (
            <li key={option.id ?? `place-${index}`} role="presentation">
              <button
                type="button"
                id={`${listId}-option-${index}`}
                role="option"
                aria-selected={index === highlighted}
                className={
                  "suggestion" +
                  (option.kind === "place" ? " suggestion--address" : "") +
                  (index === highlighted ? " suggestion--active" : "")
                }
                onMouseEnter={() => setHighlighted(index)}
                onClick={() => choose(option)}
              >
                <span className="suggestion__name">{option.label}</span>
                <span className="suggestion__meta">
                  {option.kind === "place"
                    ? "address"
                    : option.routes?.length
                      ? option.routes.slice(0, 4).join(" · ")
                      : option.id?.split(":")[0]}
                </span>
              </button>
            </li>
          ))}
          {!hasSuggestions && (
            <li className="suggestions__empty" role="presentation">
              {loading ? "Searching…" : (suggestions.error ?? "Nothing found")}
            </li>
          )}
        </ul>
      )}
    </div>
  );
}
