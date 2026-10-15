/**
 * `ScrollToHash` runs `scrollToFragment` on the address's fragment once, after
 * the page has mounted. Node renders no effects, so `useEffect` is run where the
 * test says, with the dependency list kept to be read; the page and its
 * address are stood in for by `window.location` and `document`. What is decided
 * (which element, when there is none) is `lib/fragment-scroll.test.ts`'s; this is
 * that the component asks, and asks once.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

const effects: Array<{ deps: unknown }> = [];

vi.mock("react", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react")>()),
  useEffect: (effect: () => void, deps?: unknown) => {
    effects.push({ deps });
    effect();
  },
}));

import { ScrollToHash } from "./scroll-to-hash";

/** An address and a page: the ids it draws, and every scroll that was asked for. */
function visit(hash: string, ids: string[]) {
  const scrolled: Array<{ id: string; options: unknown }> = [];
  vi.stubGlobal("window", { location: { hash } });
  vi.stubGlobal("document", {
    getElementById: (id: string) =>
      ids.includes(id)
        ? { scrollIntoView: (options: unknown) => scrolled.push({ id, options }) }
        : null,
  });
  return scrolled;
}

afterEach(() => {
  vi.unstubAllGlobals();
  effects.length = 0;
});

describe("the page's scroll to its fragment", () => {
  it("scrolls the card the address names into view when the page mounts, and draws nothing", () => {
    const scrolled = visit("#posting-mix", ["posting-mix"]);
    expect(ScrollToHash()).toBeNull();
    expect(scrolled).toEqual([{ id: "posting-mix", options: { block: "start" } }]);
  });

  it("asks once, after mount, and not on every render", () => {
    visit("#posting-mix", ["posting-mix"]);
    ScrollToHash();
    expect(effects).toHaveLength(1);
    expect(effects[0].deps).toEqual([]);
  });

  it("scrolls nothing for an address with no hash, another hash, or a card this page did not draw", () => {
    expect(visit("", ["posting-mix"])).toEqual([]);
    ScrollToHash();
    const other = visit("#somewhere-else", ["posting-mix"]);
    ScrollToHash();
    expect(other).toEqual([]);
    const absent = visit("#posting-mix", []);
    ScrollToHash();
    expect(absent).toEqual([]);
    // The positive control: the same component, an address that names a card the page has.
    const present = visit("#posting-mix", ["posting-mix"]);
    ScrollToHash();
    expect(present).toHaveLength(1);
  });
});
