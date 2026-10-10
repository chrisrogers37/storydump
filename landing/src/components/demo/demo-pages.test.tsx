/**
 * The sample's three pages over a sample the visit has built (#1649).
 *
 * The prerendered page holds a placeholder (`demo-provider.test.tsx`), so what
 * a visitor sees exists only once their browser has built the sample. Rendered
 * here to a string over `DemoSession`, the state a visit holds, which is as
 * far as this suite's `environment: "node"` goes: it shows each page draws
 * from a built sample and says what it should, not that a browser took it over.
 */

import { createElement, type FunctionComponent } from "react";
import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { DemoCalendar } from "./demo-calendar";
import { DemoOverview } from "./demo-overview";
import { DemoSession } from "./demo-provider";
import { DemoQueue } from "./demo-queue";

const render = (page: FunctionComponent) =>
  renderToString(createElement(DemoSession, null, createElement(page)));

/** How many elements hold exactly this text. */
const count = (html: string, text: string) => html.split(`>${text}<`).length - 1;

describe("the sample's pages, once the visit has built its sample", () => {
  it("the Queue lists six stories waiting for a tap and three scheduled, with a new workspace's levers", () => {
    const html = render(DemoQueue);
    expect(count(html, "awaiting approval")).toBe(6);
    expect(count(html, "scheduled")).toBe(3);
    for (const lever of ["Posted myself", "Skip", "Reject"]) expect(count(html, lever), lever).toBe(6);
    expect(count(html, "Approve")).toBe(0);
    expect(html).toContain("London time");
    expect(html).not.toContain("Europe/London");
  });

  it("the Overview is all clear beside those six, and lists ten finished stories", () => {
    const html = render(DemoOverview);
    expect(html).toContain("Nothing needs your attention");
    expect(html).toContain("no post needs attention.");
    expect(html).not.toContain("waiting on a decision");
    expect(count(html, "posted") + count(html, "skipped") + count(html, "rejected")).toBe(10);
  });

  it("the Calendar says what it draws, under the real Calendar's legend", () => {
    const html = render(DemoCalendar);
    expect(html).toContain("What posted this month, what waits in the Queue, and what is scheduled next.");
    for (const lane of ["Posted", "In Queue", "Predicted"]) expect(html, lane).toContain(lane);
  });
});
