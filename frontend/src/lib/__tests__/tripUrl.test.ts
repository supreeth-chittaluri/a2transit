import { beforeEach, describe, expect, it } from "vitest";

import type { Endpoint } from "../endpoints";
import { decodeEndpoint, encodeEndpoint, readTripFromUrl, writeTripToUrl } from "../tripUrl";

const STOP: Endpoint = {
  kind: "stop",
  label: "Temp BTC endpt",
  id: "theride:1605",
  lat: 42.2804,
  lon: -83.7461,
};

const PLACE: Endpoint = {
  kind: "place",
  label: "Michigan Stadium, South Main Street",
  lat: 42.2658285,
  lon: -83.7478267,
};

describe("encoding an endpoint", () => {
  it("writes a stop as the id the API already accepts", () => {
    expect(encodeEndpoint(STOP)).toBe("theride:1605");
  });

  it("carries a place's label, which the API never sees", () => {
    // The API knows a place only by its coordinates, so a link without the
    // label comes back reading "42.26583, -83.74783".
    expect(encodeEndpoint(PLACE)).toContain("Michigan Stadium");
  });

  it("round-trips a place", () => {
    const decoded = decodeEndpoint(encodeEndpoint(PLACE));

    expect(decoded?.kind).toBe("place");
    expect(decoded?.label).toBe(PLACE.label);
    expect(decoded?.lat).toBeCloseTo(PLACE.lat, 4);
    expect(decoded?.lon).toBeCloseTo(PLACE.lon, 4);
  });

  it("round-trips a place whose name contains a comma", () => {
    // "Kerrytown Market, Ann Arbor" splits on the same character the
    // coordinates do, so the label has to be rejoined rather than take [2].
    const commas: Endpoint = { ...PLACE, label: "Kerrytown Market, Ann Arbor, MI" };

    expect(decodeEndpoint(encodeEndpoint(commas))?.label).toBe(
      "Kerrytown Market, Ann Arbor, MI",
    );
  });

  it("round-trips a stop", () => {
    const decoded = decodeEndpoint(encodeEndpoint(STOP));

    expect(decoded?.kind).toBe("stop");
    expect(decoded?.id).toBe("theride:1605");
  });
});

describe("decoding rubbish", () => {
  it.each(["", "   ", "not-an-endpoint", "abc,def"])("returns null for %o", (input) => {
    expect(decodeEndpoint(input)).toBeNull();
  });
});

describe("reading a trip out of the URL", () => {
  it("reads all three parameters", () => {
    const trip = readTripFromUrl(
      "?from=theride:1605&to=mbus:207&depart=2026-09-10T09:00",
    );

    expect(trip.origin?.id).toBe("theride:1605");
    expect(trip.destination?.id).toBe("mbus:207");
    expect(trip.depart).toBe("2026-09-10T09:00");
  });

  it("tolerates a half-filled link", () => {
    const trip = readTripFromUrl("?from=theride:1605");

    expect(trip.origin?.id).toBe("theride:1605");
    expect(trip.destination).toBeNull();
  });

  it("tolerates no query at all", () => {
    expect(readTripFromUrl("")).toEqual({ origin: null, destination: null, depart: null });
  });
});

describe("writing a trip into the URL", () => {
  beforeEach(() => {
    window.history.replaceState(null, "", "/");
  });

  it("survives a round trip through the address bar", () => {
    writeTripToUrl({ origin: STOP, destination: PLACE, depart: "2026-09-10T09:00" });

    const trip = readTripFromUrl(window.location.search);
    expect(trip.origin?.id).toBe(STOP.id);
    expect(trip.destination?.label).toBe(PLACE.label);
    expect(trip.depart).toBe("2026-09-10T09:00");
  });

  it("clears the query when there is no trip", () => {
    writeTripToUrl({ origin: STOP, destination: PLACE, depart: null });
    writeTripToUrl({ origin: null, destination: null, depart: null });

    expect(window.location.search).toBe("");
  });

  it("replaces rather than pushes history", () => {
    // Every keystroke that completes a plan would otherwise be a history
    // entry, and Back would walk the rider through their own typing.
    const before = window.history.length;

    writeTripToUrl({ origin: STOP, destination: PLACE, depart: "2026-09-10T09:00" });

    expect(window.history.length).toBe(before);
  });
});
