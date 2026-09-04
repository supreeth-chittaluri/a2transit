import { describe, expect, it } from "vitest";

import { formatDuration, legColor, toLocalIso, toQueryValue } from "../endpoints";

describe("toQueryValue", () => {
  it("sends a stop as its composite id", () => {
    expect(
      toQueryValue({ kind: "stop", label: "x", id: "mbus:207", lat: 1, lon: 2 }),
    ).toBe("mbus:207");
  });

  it("sends a place as lat,lon", () => {
    expect(toQueryValue({ kind: "place", label: "x", lat: 42.28, lon: -83.74 })).toBe(
      "42.28,-83.74",
    );
  });
});

describe("toLocalIso", () => {
  it("stays local rather than converting to UTC", () => {
    // toISOString() would shift a 09:00 departure in Ann Arbor to 13:00 and
    // show the rider the wrong afternoon's buses.
    const morning = new Date(2026, 8, 10, 9, 0);

    expect(toLocalIso(morning)).toBe("2026-09-10T09:00");
  });

  it("pads single digits", () => {
    expect(toLocalIso(new Date(2026, 0, 5, 6, 7))).toBe("2026-01-05T06:07");
  });
});

describe("formatDuration", () => {
  it.each([
    [0, "0 min"],
    [90, "2 min"],
    [3540, "59 min"],
    [3600, "1 h 00"],
    [4500, "1 h 15"],
  ])("renders %i seconds as %s", (seconds, expected) => {
    expect(formatDuration(seconds)).toBe(expected);
  });
});

describe("legColor", () => {
  it("uses the route colour when the feed supplies one", () => {
    expect(legColor("#123456", "theride")).toBe("#123456");
  });

  it("falls back to an agency colour rather than a default grey", () => {
    expect(legColor(null, "mbus")).toBe("#00274c");
    expect(legColor(null, "theride")).toBe("#c8102e");
  });
});
