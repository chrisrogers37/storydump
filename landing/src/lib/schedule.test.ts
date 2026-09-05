import { describe, expect, it } from "vitest";
import { slotLabels, timeZoneOptions } from "./schedule";

describe("slotLabels — the clock's slots, said in the workspace's own hours", () => {
  it("spreads posts evenly across a window that wraps midnight (fn_next_slot's math)", () => {
    expect(slotLabels(14, 2, 3)).toEqual(["2:00 PM", "6:00 PM", "10:00 PM"]);
  });
  it("start == end is a 24-hour window", () => {
    expect(slotLabels(9, 9, 4)).toEqual([
      "9:00 AM",
      "3:00 PM",
      "9:00 PM",
      "3:00 AM",
    ]);
  });
  it("one post a day goes out at the window start", () => {
    expect(slotLabels(9, 21, 1)).toEqual(["9:00 AM"]);
  });
  it("shows the half hours a division produces", () => {
    expect(slotLabels(9, 12, 2)).toEqual(["9:00 AM", "10:30 AM"]);
  });
  it("says nothing for an unset or nonsensical schedule", () => {
    expect(slotLabels(null, 2, 3)).toEqual([]);
    expect(slotLabels(14, 2, 0)).toEqual([]);
  });
});

describe("timeZoneOptions — every zone the browser knows, and the saved one", () => {
  it("always lists UTC and the current value, sorted, without duplicates", () => {
    const options = timeZoneOptions("Mars/Olympus_Mons");
    expect(options).toContain("UTC");
    expect(options).toContain("Mars/Olympus_Mons");
    expect(new Set(options).size).toBe(options.length);
    expect([...options].sort()).toEqual(options);
  });
  it("carries the browser's list when it has one", () => {
    expect(timeZoneOptions("UTC")).toContain("America/New_York");
  });
});
