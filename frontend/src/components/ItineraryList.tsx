import type { Itinerary } from "../lib/api";
import { ItineraryCard } from "./ItineraryCard";

interface Props {
  itineraries: Itinerary[];
  selected: number;
  onSelect: (index: number) => void;
  queryMs: number;
  destinationLabel: string;
  onShowDepartures: (stopId: string, stopName: string) => void;
}

export function ItineraryList({
  itineraries,
  selected,
  onSelect,
  queryMs,
  destinationLabel,
  onShowDepartures,
}: Props) {
  return (
    <section className="results" aria-label="Itineraries">
      <header className="results__header">
        <span className="results__count">
          {itineraries.length} option{itineraries.length === 1 ? "" : "s"}
          {itineraries.length > 1 && " — fewest changes first"}
        </span>
        {/* The engine time, not the round trip. Worth showing: it is the claim
            the whole project rests on. */}
        <span className="results__timing" title="Server-side routing time">
          {queryMs.toFixed(1)} ms
        </span>
      </header>

      {itineraries.map((itinerary, index) => (
        <ItineraryCard
          key={`${itinerary.departure}-${itinerary.arrival}-${index}`}
          itinerary={itinerary}
          index={index}
          selected={index === selected}
          onSelect={() => onSelect(index)}
          destinationLabel={destinationLabel}
          onShowDepartures={onShowDepartures}
        />
      ))}
    </section>
  );
}
