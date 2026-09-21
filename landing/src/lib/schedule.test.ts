import { describe, expect, it } from "vitest";
import {
  postingIntervalMinutes,
  slotLabels,
  timeZoneOptions,
} from "./schedule";

/** "10:30 AM" back to minutes since midnight, so a gap can be measured. */
function minutesOf(label: string): number {
  const parts = label.match(/^(\d+):(\d+) (AM|PM)$/);
  expect(parts, `unparseable slot label: ${label}`).not.toBeNull();
  const [, h, m, suffix] = parts!;
  return ((Number(h) % 12) + (suffix === "PM" ? 12 : 0)) * 60 + Number(m);
}

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

describe("postingIntervalMinutes — the Calendar's Posting Rate (#1367)", () => {
  // The Calendar recomputed `end - start` itself and guarded on `> 0`, so
  // both shapes `fn_next_slot` handles fell through the guard and the card
  // read "interval not set". 14 → 2 is the schema DEFAULT, so that is what a
  // new workspace sees on a page whose whole job is to say when posts go out.
  it("answers for a window that wraps midnight", () => {
    // 14:00–02:00 is 12 hours; 3 posts is one every 4 hours.
    expect(postingIntervalMinutes(14, 2, 3)).toBe(240);
  });
  it("answers for a 24-hour window", () => {
    // start == end is 24 hours, the same reading `slotLabels` takes.
    expect(postingIntervalMinutes(9, 9, 4)).toBe(360);
  });
  it("agrees with the slots it is describing", () => {
    // The card and the slot list are two renderings of one schedule, so the
    // interval must BE the gap between consecutive labels — measured, not
    // asserted in a comment. This is the cross-check that would have caught
    // the original divergence: the two arithmetics disagreed for exactly the
    // shapes below, and nothing compared them.
    const shapes: [number, number, number][] = [
      [9, 23, 20], // ordinary
      [14, 2, 3], // wraps midnight — the schema default
      [9, 9, 4], // 24 hours
      [9, 12, 2], // a division that lands on a half hour
    ];
    for (const [start, end, perDay] of shapes) {
      const labels = slotLabels(start, end, perDay);
      const where = `${start}:00-${end}:00 x ${perDay}`;
      expect(labels.length, where).toBe(perDay);
      const gap =
        (minutesOf(labels[1]) - minutesOf(labels[0]) + 24 * 60) % (24 * 60);
      expect(gap, where).toBe(postingIntervalMinutes(start, end, perDay));
    }
  });
  it("still answers for an ordinary window", () => {
    expect(postingIntervalMinutes(9, 21, 4)).toBe(180);
  });
  it("has no answer for an unset or nonsensical schedule", () => {
    // Null config means no answer, not zero — the card's existing reading.
    expect(postingIntervalMinutes(null, 2, 3)).toBeNull();
    expect(postingIntervalMinutes(14, null, 3)).toBeNull();
    expect(postingIntervalMinutes(14, 2, null)).toBeNull();
    expect(postingIntervalMinutes(14, 2, 0)).toBeNull();
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
