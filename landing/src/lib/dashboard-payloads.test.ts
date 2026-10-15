import { describe, it, expect } from "vitest";
import {
  deriveFolderMix,
  derivePoolHealth,
  deriveSummary,
  type StatsResponse,
} from "./dashboard-payloads";
import type { MixSourceRow } from "./category-mix";

/**
 * The derivations over `stats` (#1044).
 *
 * `stats` returns flat count dicts; the screens need rates and joins. That
 * arithmetic is the front end's, so it is pinned here rather than trusted
 * inline in six components.
 */
const stats = (over: Partial<StatsResponse> = {}): StatsResponse => ({
  intents_by_state: {},
  media_by_state: {},
  media_never_posted: 0,
  media_by_category: {},
  posted_by_source: {},
  posts_by_day: [],
  accounts: 0,
  sources: 0,
  ...over,
});

describe("deriveSummary", () => {
  it("counts states and rates publish outcomes only", () => {
    const s = deriveSummary(
      stats({
        intents_by_state: {
          posted: 8,
          failed: 2,
          skipped: 5,
          rejected: 1,
          scheduled: 4,
        },
        posts_by_day: [
          { local_date: "2026-08-01", count: 3, cap: 5 },
          { local_date: "2026-08-02", count: 5, cap: 5 },
        ],
      }),
    );
    expect(s.posted).toBe(8);
    expect(s.failed).toBe(2);
    expect(s.skipped).toBe(5);
    expect(s.rejected).toBe(1);
    // Every state, including the ones no card shows.
    expect(s.total).toBe(20);
    // posted / (posted + failed) — NOT posted / total. Skipped and rejected are
    // approval outcomes, not failed publishes; putting them in the divisor is
    // the #466 lumping this deliberately avoids.
    expect(s.success_rate).toBeCloseTo(0.8);
    expect(s.avg_per_day).toBeCloseTo(4);
  });

  /**
   * Zero is not safe on a rate (#1090) — `0%` on the card reads as a verdict
   * on the workspace rather than as there being nothing to judge — so both
   * are withheld, and no NaN escapes either divisor.
   */
  it("withholds both rates on an empty workspace rather than reporting zero", () => {
    const s = deriveSummary(stats());
    expect(s.success_rate).toBeNull();
    expect(s.avg_per_day).toBeNull();
    // No NaN escapes either divisor.
    expect(Number.isNaN(s.success_rate as number)).toBe(false);
    expect(Number.isNaN(s.avg_per_day as number)).toBe(false);
  });

  it("reports a REAL zero rate, which is a different fact from no attempts", () => {
    // Withholding must not swallow a genuine 0%. A
    // workspace that attempted twice and failed twice HAS a success rate, and
    // it is zero — that is a measurement and must render as one.
    const s = deriveSummary(
      stats({
        intents_by_state: { failed: 2 },
        posts_by_day: [{ local_date: "2026-08-01", count: 0, cap: 5 }],
      }),
    );
    expect(s.success_rate).toBe(0);
    expect(s.avg_per_day).toBe(0);
  });
});

const folder = (
  over: Partial<MixSourceRow> & { source_id: string },
): MixSourceRow => ({
  provider: "gdrive",
  name: over.source_id,
  state: "active",
  media_count: 1,
  ratio: null,
  effective: 0,
  ...over,
});

describe("deriveFolderMix", () => {
  const mix = {
    rows: [
      folder({ source_id: "s-events", name: "events", ratio: 0.7, effective: 70 }),
      folder({ source_id: "s-tips", name: "tips", ratio: null, effective: 30 }),
      folder({ source_id: "s-old", name: "old", ratio: 0, effective: 0 }),
    ],
  };

  it("sets each folder's plan beside its share of every post in the window", () => {
    const { folders, total, fromRemoved } = deriveFolderMix(
      stats({ posted_by_source: { "s-events": 6, "s-tips": 2, "s-gone": 2 } }),
      mix,
    );
    expect(folders.map((f) => [f.name, f.mode, f.planned])).toEqual([
      ["events", "explicit", 70],
      ["tips", "automatic", 30],
      ["old", "off", 0],
    ]);
    // Out of all ten posts, the two from a folder no longer connected included.
    expect(folders[0].posted).toBeCloseTo(60);
    expect(folders[1].posted).toBeCloseTo(20);
    expect({ total, fromRemoved }).toEqual({ total: 10, fromRemoved: 2 });
  });

  it("gives a folder with no post in the window a real 0%", () => {
    const { folders } = deriveFolderMix(
      stats({ posted_by_source: { "s-events": 3 } }),
      mix,
    );
    expect(folders.map((f) => f.posted)).toEqual([100, 0, 0]);
  });

  it("withholds every posted share when the window holds no post", () => {
    const { folders, total } = deriveFolderMix(stats(), mix);
    expect(total).toBe(0);
    for (const f of folders) {
      expect(f.posted, `${f.name} must be withheld`).toBeNull();
      expect(typeof f.posted, `${f.name} must not be a number`).not.toBe("number");
    }
  });

  it("reads an API that sent no posted_by_source as unavailable, not as no posts", () => {
    const older: Partial<StatsResponse> = stats();
    delete older.posted_by_source;
    const { folders, total, fromRemoved } = deriveFolderMix(
      older as StatsResponse,
      mix,
    );
    expect(total).toBeNull();
    expect(fromRemoved).toBe(0);
    for (const f of folders) {
      expect(f.posted, `${f.name} must be withheld`).toBeNull();
    }
  });

  it("joins on source_id, never on the folder's name", () => {
    const { folders, fromRemoved } = deriveFolderMix(
      stats({ posted_by_source: { events: 5 } }),
      mix,
    );
    expect(folders[0].posted).toBe(0);
    expect(fromRemoved).toBe(5);
  });
});

describe("derivePoolHealth", () => {
  it("counts what stats serves", () => {
    const h = derivePoolHealth(
      stats({
        media_by_state: { available: 12, removed: 3 },
        media_never_posted: 5,
        media_by_category: { coffee: 7, pastry: 5 },
      }),
    );
    expect(h.total_active).toBe(12);
    expect(h.never_posted).toBe(5);
    expect(h.by_category).toEqual([
      { name: "coffee", count: 7 },
      { name: "pastry", count: 5 },
    ]);
  });
});

/**
 * THE GUARD ON THE WITHHELD FIGURES (#1048).
 *
 * These three figures have no target-side source, so they must be `null`: `0`
 * renders as a measurement — "nothing is reused", "nothing is eligible" —
 * which is a claim about the workspace made from a missing column.
 *
 * `toBeNull` alone would pass for `undefined`, and `undefined` is what a
 * refactor to optional fields would produce on its way to `?? 0`. So each is
 * asserted null AND asserted not to be a number: a silent version reddens
 * here before it reaches a screen.
 */
describe("the unsourced figures stay withheld", () => {
  const populated = stats({
    media_by_state: { available: 9 },
    media_never_posted: 2,
    media_by_category: { coffee: 9 },
    posted_by_source: { "s-coffee": 4 },
  });

  it("keeps the pool buckets null on a workspace with real data", () => {
    const h = derivePoolHealth(populated);
    for (const [name, value] of [
      ["posted_once", h.posted_once],
      ["posted_multiple", h.posted_multiple],
      ["eligible_for_posting", h.eligible_for_posting],
    ] as const) {
      expect(value, `${name} must be withheld`).toBeNull();
      expect(typeof value, `${name} must not be a number`).not.toBe("number");
    }
  });
});

/**
 * The state sets are tested in `intent-states-contract.test.ts`, against the
 * API's own constant. Membership ("every name is real") and one-way exclusion
 * ("no terminal state is queued") both pass with a non-terminal state missing
 * from the queue entirely, so it asserts EXHAUSTIVENESS: a set that is valid
 * but short fails loudly.
 */
