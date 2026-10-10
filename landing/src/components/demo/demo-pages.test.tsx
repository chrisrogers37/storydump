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
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/** The page's query string, which the Calendar reads its open day from. */
const search = vi.hoisted(() => ({ value: "" }));
vi.mock("next/navigation", async (original) => ({
  ...(await original<typeof import("next/navigation")>()),
  useSearchParams: () => new URLSearchParams(search.value),
}));

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

describe("the sample's Calendar opens its days", () => {
  // 15:21 in London on Saturday 10 October 2026. Four of the day's six slots
  // have arrived and wait for a tap; the 5:00 PM and 7:00 PM stories are
  // scheduled. The month draws three of the six and "+3 more".
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-10-10T14:21:00Z"));
    search.value = "";
  });
  afterEach(() => {
    vi.useRealTimers();
    search.value = "";
  });

  const TODAY = [
    "monday-mood.jpg",
    "studio-desk.jpg",
    "spring-colours.jpg",
    "when-the-printer-works.jpg",
    "close-up-texture.jpg",
    "team-lunch.jpg",
  ];
  /** The day view's own heading. A day's link in the month says the date too, in its label. */
  const HEADING = ">Saturday, October 10<";
  /** The day view, which is drawn after the month. */
  const dayView = (html: string) => html.slice(html.indexOf(HEADING));

  it("keeps half of the day's stories under \"+3 more\" in the month", () => {
    const html = render(DemoCalendar);
    // React writes "+", the count and " more" as three text nodes.
    expect(html).toMatch(/\+(?:<!-- -->)?3(?:<!-- -->)? more/);
    expect(html).not.toContain("close-up-texture.jpg");
    expect(html).not.toContain("team-lunch.jpg");
  });

  it("makes each day a link to its day view, and draws no other month", () => {
    const html = render(DemoCalendar);
    expect(html).toContain('href="?month=2026-10&amp;day=2026-10-10"');
    expect(html).not.toContain("Previous month");
    expect(html).not.toContain("Next month");
    expect(html).not.toContain(HEADING);
  });

  it("lists every story of the open day, so each one the Queue names can be reached", () => {
    search.value = "month=2026-10&day=2026-10-10";
    const html = render(DemoCalendar);
    expect(html).toContain(HEADING);
    for (const file of TODAY) expect(count(dayView(html), file), file).toBe(1);
    expect(count(dayView(html), "awaiting approval")).toBe(4);
    expect(count(dayView(html), "scheduled")).toBe(2);
    expect(count(dayView(html), "Close")).toBe(1);
  });

  it("opens no day the month does not draw", () => {
    search.value = "month=2026-10&day=2026-11-20";
    const html = render(DemoCalendar);
    expect(html).not.toContain(">Close<");
  });
});
