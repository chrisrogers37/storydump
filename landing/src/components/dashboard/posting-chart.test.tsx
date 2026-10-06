/**
 * The posting chart labels each bar with the day its row names (#1511).
 * `posts_by_day.local_date` is the workspace's own date; read through `Date`
 * it became UTC midnight, the day before west of Greenwich. This file runs
 * with the process clock in New York, so it fails on that reading even where
 * CI's own clock is UTC. Read as a returned element tree, without a DOM.
 */

import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { isValidElement, type ReactElement } from "react";
import { BarChart } from "recharts";
import { PostingChart } from "./posting-chart";

const zone = process.env.TZ;
beforeAll(() => {
  process.env.TZ = "America/New_York";
});
afterAll(() => {
  if (zone === undefined) delete process.env.TZ;
  else process.env.TZ = zone;
});

/** Every element in a returned tree, depth-first. */
function* walk(node: unknown): Generator<ReactElement> {
  if (Array.isArray(node)) {
    for (const child of node) yield* walk(child);
    return;
  }
  if (!isValidElement(node)) return;
  yield node;
  yield* walk((node.props as { children?: unknown }).children);
}

describe("the posting chart's day labels", () => {
  it("runs west of Greenwich, where the old reading shifts a day", () => {
    // The positive control: without it, a clock that stayed in UTC would
    // pass the next test on the old code too.
    expect(new Date("2026-09-03").getDate()).toBe(2);
  });

  it("labels each bar with the day its row names", () => {
    const tree = PostingChart({
      data: [
        { local_date: "2026-09-03", count: 4, cap: 6 },
        { local_date: "2026-10-01", count: 2, cap: 6 },
      ],
    }) as ReactElement;
    const chart = [...walk(tree)].find((el) => el.type === BarChart);
    expect(chart, "the chart renders a <BarChart>").toBeDefined();
    const labels = (chart!.props as { data: { label: string }[] }).data.map((d) => d.label);
    expect(labels).toEqual(["Sep 3", "Oct 1"]);
  });
});
