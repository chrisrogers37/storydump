/**
 * Recent Activity reads each time on the workspace's clock (#1511). Read on
 * the clock the code ran on, the server's HTML (UTC) and the browser's render
 * disagreed outside UTC, and React threw error 418 on every Overview load.
 * The same row is rendered here under three process clocks and must read the
 * same on each. Read as a returned element tree, without a DOM.
 */

import { afterAll, describe, expect, it } from "vitest";
import { isValidElement } from "react";
import { RecentActivity } from "./recent-activity";

const zone = process.env.TZ;
afterAll(() => {
  if (zone === undefined) delete process.env.TZ;
  else process.env.TZ = zone;
});

/** 9:30 PM on Thursday, Oct 1 in New York; 1:30 AM on Friday, Oct 2 in UTC. */
const ROW = {
  id: "i1",
  state: "posted",
  file_name: "a.jpg",
  category: "memes",
  entered_state_at: "2026-10-02T01:30:00.123456+00:00",
} as const;

/** Every string in a returned tree, depth-first. */
function* text(node: unknown): Generator<string> {
  if (typeof node === "string") {
    yield node;
    return;
  }
  if (Array.isArray(node)) {
    for (const child of node) yield* text(child);
    return;
  }
  if (!isValidElement(node)) return;
  yield* text((node.props as { children?: unknown }).children);
}

/** ICU puts a narrow no-break space before AM/PM; compare words, not bytes. */
const spaced = (s: string) => s.replace(/\s/g, " ");

describe.each([
  ["UTC", 1],
  ["America/New_York", 21],
  ["Asia/Tokyo", 10],
])("rendered on a %s clock", (clock, hour) => {
  it("reads the row's time on the workspace's clock, whatever clock renders it", () => {
    process.env.TZ = clock;
    // The positive control: the process clock really moved, or a run that
    // stayed on one clock would pass on the old reading too.
    expect(new Date(ROW.entered_state_at).getHours()).toBe(hour);
    const strings = [...text(RecentActivity({ items: [ROW], tz: "America/New_York" }))];
    expect(strings.map(spaced)).toContain("Oct 1, 9:30 PM");
  });
});

describe("without a zone", () => {
  // The zone is optional, so a caller without one still builds. Such a
  // caller gets the clock the code runs on, as `when` says.
  it.each([
    ["America/New_York", "Oct 1, 9:30 PM"],
    ["UTC", "Oct 2, 1:30 AM"],
  ])("reads the %s clock it runs on", (clock, shown) => {
    process.env.TZ = clock;
    const strings = [...text(RecentActivity({ items: [ROW] }))];
    expect(strings.map(spaced)).toContain(shown);
  });
});
