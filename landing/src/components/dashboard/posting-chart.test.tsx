/**
 * The posting chart labels each bar with the day its row names (#1511), and
 * heads its tooltip with that day's weekday and date.
 * `posts_by_day.local_date` is the workspace's own date; read through `Date`
 * it became UTC midnight, the day before west of Greenwich. This file runs
 * with the process clock in New York, so it fails on that reading even where
 * CI's own clock is UTC. Read as a returned element tree, without a DOM.
 */

import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { isValidElement, type ReactElement } from "react";
import { BarChart, Tooltip } from "recharts";
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

describe("the posting chart's tooltip", () => {
  it("names the weekday of the day its bar names, on this clock too", () => {
    // 9 Oct 2026 is a Friday. Read as UTC midnight on this process's clock it
    // is Thursday evening, the 8th, so a weekday taken from that instant would
    // be a day early: it must come from the bar's own day, as the date does.
    const tree = PostingChart({
      data: [
        { local_date: "2026-10-09", count: 12, cap: 12 },
        { local_date: "2026-11-01", count: 3, cap: 6 },
      ],
    }) as ReactElement;
    const elements = [...walk(tree)];
    const chart = elements.find((el) => el.type === BarChart);
    const tooltip = elements.find((el) => el.type === Tooltip);
    expect(tooltip, "the chart renders a <Tooltip>").toBeDefined();
    const heading = (
      tooltip!.props as { labelFormatter: (label: string, payload: unknown[]) => unknown }
    ).labelFormatter;
    const rows = (chart!.props as { data: { label: string }[] }).data;
    expect(rows.map((row) => heading(row.label, [{ payload: row }]))).toEqual([
      "Fri, Oct 9",
      "Sun, Nov 1",
    ]);
    // The axis keeps the date alone.
    expect(rows.map((row) => row.label)).toEqual(["Oct 9", "Nov 1"]);
  });
});
