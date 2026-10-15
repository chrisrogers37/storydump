/**
 * An opened day is put in front of the visitor, and the visitor is handed back
 * to that day when its view closes, unless they have put focus somewhere else.
 *
 * This suite has no DOM (`vitest.config.ts`), so nothing in it watches a
 * scroll or a focus move. Read here: what the day view asks of the elements it
 * is given, in order; when React runs those steps, with the title's hooks
 * stubbed; and what its title writes into the page.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const hoisted = vi.hoisted(() => ({
  ref: { current: null as unknown },
  effects: [] as { run: () => unknown; deps?: readonly unknown[] }[],
}));

vi.mock("react", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react")>();
  return {
    ...actual,
    useRef: () => hoisted.ref,
    useEffect: (run: () => unknown, deps?: readonly unknown[]) =>
      void hoisted.effects.push({ run, deps }),
  };
});

import { createElement } from "react";
import { renderToString } from "react-dom/server";
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
  it("returns focus that went with it to the day's link drawn at this width, and scrolls only as far as it must", () => {
    const log: string[] = [];
    // The month draws a day twice: a list row for phones, a grid cell for wider screens.
    returnToDay([element(log, "row", { drawn: false }), element(log, "cell")], false);
    expect(log).toEqual([
      'cell.focus {"preventScroll":true}',
      'cell.scrollIntoView {"block":"nearest"}',
    ]);
  });

  it("asks nothing when the visitor has put focus on a control still in the page", () => {
    const log: string[] = [];
    returnToDay([element(log, "cell")], true);
    expect(log).toEqual([]);
  });

  it("asks nothing of a month that has left the page with it", () => {
    const log: string[] = [];
    returnToDay(
      [element(log, "row", { connected: false }), element(log, "cell", { connected: false })],
      false,
    );
    expect(log).toEqual([]);
  });

  it("asks nothing when the month marked no day", () => {
    expect(() => returnToDay([], false)).not.toThrow();
  });
});

describe("the day view's title, as React runs its effect", () => {
  const log: string[] = [];
  const card = element(log, "card");
  /** The page: where focus is, and the links the month marks while a day is open. */
  const page = {
    body: { isConnected: true },
    activeElement: null as unknown,
    marked: [] as unknown[],
    querySelectorAll: (selector: string): unknown[] =>
      selector === 'a[aria-current="date"]' ? page.marked : [],
  };

  /**
   * Draws the title as React runs one effect: on the first draw, then on a draw
   * whose list has changed (`Object.is` on each entry; no list is a change every
   * time), after the last run's clean-up, which runs again when the title goes.
   */
  function drawTitle(date: string) {
    let last: { deps?: readonly unknown[]; cleanUp: unknown } | undefined;
    const close = () => {
      const cleanUp = last?.cleanUp;
      if (typeof cleanUp === "function") cleanUp();
    };
    const draw = (next: string) => {
      hoisted.effects.length = 0;
      OpenedDayTitle({ date: next });
      const { run, deps } = hoisted.effects[0];
      const prev = last?.deps;
      if (prev && deps?.length === prev.length && deps.every((d, i) => Object.is(d, prev[i]))) return;
      close();
      last = { deps, cleanUp: run() };
    };
    draw(date);
    return { draw, close };
  }

  beforeEach(() => {
    page.activeElement = null;
    page.marked = [element(log, "row", { drawn: false }), element(log, "cell")];
    hoisted.ref.current = {
      ...element(log, "title"),
      closest: (selector: string) => (selector === '[data-slot="card"]' ? card : null),
    };
    vi.stubGlobal("document", page);
    log.length = 0;
  });

  afterEach(() => vi.unstubAllGlobals());

  it("shows its day by its title and its card, and not again while the same day is drawn", () => {
    const title = drawTitle("2026-10-09");
    expect(log).toEqual([
      'title.focus {"preventScroll":true}',
      'card.scrollIntoView {"block":"nearest"}',
    ]);
    log.length = 0;
    title.draw("2026-10-09");
    expect(log).toEqual([]);
  });

  it.each([
    ["body, where the browser leaves it", page.body],
    ["no element", null],
    ["an element no longer in the page", element(log, "close", { connected: false })],
  ])("hands focus that went with the view back to the day it read while open: %s", (_, active) => {
    const title = drawTitle("2026-10-09");
    log.length = 0;
    page.marked = []; // Once the day closes, the month marks no link.
    page.activeElement = active;
    title.close();
    expect(log).toEqual([
      'cell.focus {"preventScroll":true}',
      'cell.scrollIntoView {"block":"nearest"}',
    ]);
  });

  it("leaves focus on a month arrow or a nav item that the visitor pressed", () => {
    const title = drawTitle("2026-10-09");
    log.length = 0;
    page.activeElement = element(log, "arrow");
    title.close();
    expect(log).toEqual([]);
  });

  it("shows another day when it is pressed, and leaves focus on its link until then", () => {
    const title = drawTitle("2026-10-09");
    log.length = 0;
    page.activeElement = element(log, "link");
    title.draw("2026-10-10");
    expect(log).toEqual([
      'title.focus {"preventScroll":true}',
      'card.scrollIntoView {"block":"nearest"}',
    ]);
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
