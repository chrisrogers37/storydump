import { describe, expect, it } from "vitest";
import { deriveSummary } from "@/lib/dashboard-payloads";
import { sampleWorkspace } from "./fixtures";
import { overviewOf } from "./overview";
import { demoReducer, initialDemoState, type DemoAction } from "./state";

const NOW = new Date("2026-10-15T16:20:00.000Z");
const w = sampleWorkspace(NOW);

/** The Queue after the visitor's taps, each at its own time. */
function tapped(...taps: [id: string, action: DemoAction, at: string][]) {
  return taps.reduce(
    (state, [intentId, action, at]) => demoReducer(state, { type: "act", intentId, action, at }),
    initialDemoState(w.queue),
  ).queue;
}

describe("the sample's Overview", () => {
  it("before any tap, is the month as the sample built it", () => {
    const { stats, activity } = overviewOf(w, w.queue);
    expect(stats).toEqual(w.stats);
    expect(activity).toEqual(w.history);
  });

  it("moves its counts as a real workspace's would, and lists the taps first in Recent activity", () => {
    // The Overview once ignored the visitor: a skip and a reject left Skipped
    // and Recent activity as they were (#1649).
    const queue = tapped(
      ["sample-waiting-1", "mark_posted", "2026-10-15T16:21:00.000Z"],
      ["sample-waiting-2", "skip", "2026-10-15T16:22:00.000Z"],
      ["sample-waiting-3", "reject", "2026-10-15T16:23:00.000Z"],
    );
    const { stats, activity } = overviewOf(w, queue);
    const by = w.stats.intents_by_state;

    expect(stats.intents_by_state).toEqual({
      ...by,
      posted: by.posted + 1,
      skipped: by.skipped + 1,
      rejected: by.rejected + 1,
      awaiting_approval: by.awaiting_approval - 3,
    });
    expect(deriveSummary(stats).posted).toBe(by.posted + 1);

    // Marked posted today: today's bar and its folder's share each move by one.
    expect(stats.posts_by_day.at(-1)!.count).toBe(w.stats.posts_by_day.at(-1)!.count + 1);
    expect(stats.posts_by_day.slice(0, -1)).toEqual(w.stats.posts_by_day.slice(0, -1));
    expect(stats.posted_by_source).toEqual({
      ...w.stats.posted_by_source,
      "sample-folder-products": w.stats.posted_by_source["sample-folder-products"] + 1,
    });

    expect(activity.slice(0, 3).map((s) => [s.file_name, s.state, s.entered_state_at])).toEqual([
      ["monday-mood.jpg", "rejected", "2026-10-15T16:23:00.000Z"],
      ["packing-orders.mp4", "skipped", "2026-10-15T16:22:00.000Z"],
      ["new-arrivals-flat-lay.jpg", "posted", "2026-10-15T16:21:00.000Z"],
    ]);
    expect(activity.slice(3)).toEqual(w.history);
  });
});
