/**
 * An opened day is put in front of the visitor, and the visitor is handed back
 * to that day when its view closes.
 *
 * This suite runs without a DOM (`vitest.config.ts`), so nothing here watches a
 * page scroll or focus move: the rendered shots on the pull request do that.
 * Read here: what the day view asks of the elements it is given, in order, and
 * what its title writes into the page.
 */

import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { OpenedDayTitle, returnToDay, showOpenedDay } from "./opened-day";

/** An element that writes down what is asked of it. */
const element = (log: string[], name: string, { drawn = true, connected = true } = {}) => ({
  isConnected: connected,
  getClientRects: () => (drawn ? [{}] : []),
  focus: (options?: FocusOptions) => void log.push(`${name}.focus ${JSON.stringify(options)}`),
  scrollIntoView: (options?: ScrollIntoViewOptions) =>
    void log.push(`${name}.scrollIntoView ${JSON.stringify(options)}`),
});

describe("a day view that has just been drawn", () => {
  it("takes focus on its title without scrolling for it, then scrolls itself in by its nearest edge", () => {
    const log: string[] = [];
    showOpenedDay(element(log, "title"), element(log, "view"));
    expect(log).toEqual([
      'title.focus {"preventScroll":true}',
      'view.scrollIntoView {"block":"nearest"}',
    ]);
  });
});

describe("a day view that has closed", () => {
  it("returns focus to the day's link that is drawn at this width, and scrolls only as far as it must", () => {
    const log: string[] = [];
    // The month draws a day twice: a list row for phones, a grid cell for wider screens.
    returnToDay([element(log, "row", { drawn: false }), element(log, "cell")]);
    expect(log).toEqual([
      'cell.focus {"preventScroll":true}',
      'cell.scrollIntoView {"block":"nearest"}',
    ]);
  });

  it("asks nothing of a month that has left the page with it", () => {
    const log: string[] = [];
    returnToDay([
      element(log, "row", { connected: false }),
      element(log, "cell", { connected: false }),
    ]);
    expect(log).toEqual([]);
  });

  it("asks nothing when the month marked no day", () => {
    expect(() => returnToDay([])).not.toThrow();
  });
});

describe("the day view's title", () => {
  const html = () =>
    renderToString(createElement(OpenedDayTitle, { date: "2026-10-09" }, "Friday, October 9"));

  it("can be given focus, and is not a stop of the Tab key", () => {
    expect(html()).toMatch(/<div[^>]*tabindex="-1"[^>]*>Friday, October 9</);
  });

  it("is still the card's title", () => {
    expect(html()).toContain('data-slot="card-title"');
  });
});
