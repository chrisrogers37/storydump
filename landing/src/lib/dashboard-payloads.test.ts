import { describe, it, expect } from "vitest";
import {
  HISTORY_STATES,
  QUEUE_STATES,
  SCHEDULED_STATES,
  deriveCategories,
  derivePoolHealth,
  deriveSummary,
  type StatsResponse,
} from "./dashboard-payloads";

/**
 * The derivations that used to happen server-side (#1044).
 *
 * `stats` returns flat count dicts; the screens need rates and joins. That
 * arithmetic moved to the front end, so it is pinned here rather than trusted
 * inline in six components.
 */
const stats = (over: Partial<StatsResponse> = {}): StatsResponse => ({
  intents_by_state: {},
  media_by_state: {},
  media_never_posted: 0,
  media_by_category: {},
  posted_by_category: {},
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

  it("does not divide by zero on an empty workspace", () => {
    const s = deriveSummary(stats());
    expect(s.success_rate).toBe(0);
    expect(s.avg_per_day).toBe(0);
    expect(Number.isNaN(s.avg_per_day)).toBe(false);
  });
});

describe("deriveCategories", () => {
  it("joins the two dicts and shares out of posted, not out of library", () => {
    const rows = deriveCategories(
      stats({
        media_by_category: { coffee: 10, pastry: 6 },
        posted_by_category: { coffee: 3, pastry: 1 },
      }),
    );
    expect(rows.map((r) => r.category)).toEqual(["coffee", "pastry"]);
    expect(rows[0]).toMatchObject({ posted: 3, total: 10 });
    expect(rows[0].actual_ratio).toBeCloseTo(0.75);
    expect(rows[1].actual_ratio).toBeCloseTo(0.25);
  });

  it("keeps a category that has media but no posts", () => {
    const rows = deriveCategories(
      stats({ media_by_category: { unposted: 4 }, posted_by_category: {} }),
    );
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ posted: 0, total: 4, actual_ratio: 0 });
  });

  it("drops the empty-string key the SQL GROUP BY produces for NULL", () => {
    const rows = deriveCategories(
      stats({ media_by_category: { "": 3, coffee: 1 } }),
    );
    expect(rows.map((r) => r.category)).toEqual(["coffee"]);
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
 * THE GUARD ON THE DECISION THAT IS NOT OURS (#1048).
 *
 * These three figures have no target-side source. Chris rules on whether to
 * drop them or serve them; until then they must be `null`, because `0` renders
 * as a measurement — "nothing is reused", "nothing is eligible" — which is a
 * claim about the workspace made from a missing column.
 *
 * `toBeNull` alone would pass for `undefined`, and `undefined` is what a
 * refactor to optional fields would produce on its way to `?? 0`. So each is
 * asserted null AND asserted not to be a number: reintroducing the silent
 * version reddens here before it reaches a screen.
 */
describe("the contested figures stay withheld", () => {
  const populated = stats({
    media_by_state: { available: 9 },
    media_never_posted: 2,
    media_by_category: { coffee: 9 },
    posted_by_category: { coffee: 4 },
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

  it("keeps the configured mix null even where actual is known", () => {
    const rows = deriveCategories(populated);
    expect(rows[0].actual_ratio).toBeCloseTo(1);
    expect(rows[0].configured_ratio).toBeNull();
    expect(typeof rows[0].configured_ratio).not.toBe("number");
  });
});

/**
 * The state sets are sent to a route that validates against the closed
 * `ck_intent_state` vocabulary — an unknown member is a 422, so a typo here is
 * a broken screen rather than a quietly short list.
 */
describe("the intent state sets", () => {
  const VOCABULARY = new Set([
    "scheduled",
    "prompt_pending",
    "awaiting_approval",
    "approved",
    "publishing",
    "publishing_ambiguous",
    "review_required",
    "posted",
    "skipped",
    "rejected",
    "expired",
    "failed",
    "cancelled",
  ]);

  it("names only real states", () => {
    for (const set of [HISTORY_STATES, QUEUE_STATES, SCHEDULED_STATES]) {
      for (const state of set.split(",")) {
        expect(VOCABULARY.has(state), `${state} in "${set}"`).toBe(true);
      }
    }
  });

  it("does not put a terminal state in the queue", () => {
    const terminal = ["posted", "skipped", "rejected", "expired", "cancelled"];
    for (const state of QUEUE_STATES.split(",")) {
      expect(terminal, `${state} is terminal`).not.toContain(state);
    }
  });
});
