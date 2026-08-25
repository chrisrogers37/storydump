import { describe, it, expect } from "vitest";
import type { ReactElement, ReactNode } from "react";
import { Card } from "@/components/ui/card";
import { derivePoolHealth, type StatsResponse } from "@/lib/dashboard-payloads";
import { PoolHealth } from "@/components/dashboard/media/pool-health";
import MediaLoading from "./loading";

/**
 * THE SKELETON AND THE PAGE ARE A CONTRACT, and nothing was checking it.
 *
 * #1060: the loading skeleton reserved four pool-health cards while
 * `PoolHealth` renders two. The user watched four placeholders resolve into
 * two, with no way to tell design from failure. Every automated gate passed —
 * lint, tsc, build, the suite, the visual deploy — because none of them
 * compares a skeleton against what eventually renders. It was findable only by
 * looking at both, which is why it survived #1051 and was caught by rendering
 * the composed page rather than reasoning about it.
 *
 * This is that comparison, made cheap enough to run every time.
 *
 * NO DOM, deliberately. `vitest.config.ts` pins `environment: "node"` and says
 * so on purpose; both of these are plain functions returning element trees, so
 * the counts are readable from the returned structure without jsdom. Adding a
 * DOM to assert a card count would trade the config's honesty for nothing.
 */

/** Depth-first walk of a React element tree, children flattened. */
function* walk(node: ReactNode): Generator<ReactElement> {
  if (node === null || node === undefined || typeof node !== "object") return;
  if (Array.isArray(node)) {
    for (const child of node) yield* walk(child);
    return;
  }
  const el = node as ReactElement<{ children?: ReactNode }>;
  yield el;
  yield* walk(el.props?.children);
}

/**
 * The first `grid` container in a tree, and how many `Card`s it holds.
 *
 * Both trees put the pool-health row first, so "first grid" identifies the
 * same region in each without either file needing a test-only marker. The
 * media grid below it is a separate container and is not counted — which the
 * positive control at the bottom of this file proves rather than assumes.
 */
function firstGridCardCount(tree: ReactNode): number {
  for (const el of walk(tree)) {
    const cls = (el.props as { className?: unknown })?.className;
    if (typeof cls === "string" && cls.includes("grid")) {
      return [...walk(el)].filter((c) => c.type === Card).length;
    }
  }
  throw new Error("no grid container found — the layout moved, so this contract is unverified");
}

/** `stats` as the API actually serves it; the derivation supplies the nulls. */
const STATS: StatsResponse = {
  intents_by_state: {},
  media_by_state: { available: 12 },
  media_never_posted: 5,
  media_by_category: { surf: 7 },
  posted_by_category: { surf: 3 },
  posts_by_day: [],
  accounts: 1,
  sources: 1,
};

describe("the media loading skeleton matches what the page renders", () => {
  it("reserves exactly as many pool-health cards as PoolHealth renders", () => {
    const rendered = firstGridCardCount(
      PoolHealth({ health: derivePoolHealth(STATS) }),
    );
    const promised = firstGridCardCount(MediaLoading());

    expect(
      promised,
      `the skeleton promises ${promised} pool-health card(s) and the page ` +
        `renders ${rendered} — a skeleton that reserves space for content ` +
        `nobody supplies is the #1060 defect`,
    ).toBe(rendered);
  });

  it("keeps the withheld-figures note unconditional, which is why space is reserved for it", () => {
    // The skeleton reserves two lines for that note. That is only correct
    // while the note always renders — if a figure ever gains a source the
    // note shrinks or disappears and the reservation becomes its own small
    // over-promise. Pinned here so that change is visible rather than silent.
    const health = derivePoolHealth(STATS);
    expect(health.posted_multiple).toBeNull();
    expect(health.eligible_for_posting).toBeNull();
  });

  it("counts the pool row rather than the media grid (positive control)", () => {
    // Without this, a walker that accidentally counted every Card in the tree
    // would still make the assertion above pass whenever the two totals
    // happened to agree. The skeleton's media grid is 8 cards; the pool row is
    // not, so a counter that reached into the grid would read 10, not 2.
    const all = [...walk(MediaLoading())].filter((c) => c.type === Card).length;
    const pool = firstGridCardCount(MediaLoading());
    expect(all).toBeGreaterThan(pool);
    expect(pool).toBeLessThanOrEqual(4);
  });
});
