import { describe, expect, it } from "vitest";
import { deriveConditions } from "@/lib/conditions";
import { deriveFolderMix, deriveSummary } from "@/lib/dashboard-payloads";
import { sampleWorkspace } from "./fixtures";

const NOW = new Date("2026-10-15T16:20:00.000Z");
const w = sampleWorkspace(NOW);
const stories = [...w.queue, ...w.history];

describe("the sample workspace", () => {
  it("needs nothing: a raised condition would link into the real dashboard", () => {
    expect(
      deriveConditions({
        accounts: w.accounts.accounts,
        sources: w.sources.sources,
        intentsByState: w.stats.intents_by_state,
      }),
    ).toEqual([]);
  });

  it("is made up through and through: one placeholder account, generic folders, no media", () => {
    expect(w.config.name).toBe("Example Co");
    expect(w.accounts.accounts.map((a) => [a.handle, a.display_name])).toEqual([
      ["example.brand", "Example Co"],
    ]);
    expect(w.sources.sources.map((s) => s.folder_name)).toEqual([
      "Product shots",
      "Behind the scenes",
      "Memes",
    ]);
    for (const story of stories) {
      expect(story.account_handle).toBe("example.brand");
      expect(story.thumbnail_url).toBeNull();
      expect(story.caption).toBeNull();
      expect(story.ig_permalink).toBeNull();
    }
  });

  it("waits on six stories whose slots have arrived, and plans three for later, in slot order", () => {
    const waiting = w.queue.filter((i) => i.state === "awaiting_approval");
    const later = w.queue.filter((i) => i.state === "scheduled");
    expect(waiting).toHaveLength(6);
    expect(later).toHaveLength(3);
    for (const story of waiting) expect(Date.parse(story.schedule_slot_at)).toBeLessThanOrEqual(NOW.getTime());
    for (const story of later) expect(Date.parse(story.schedule_slot_at)).toBeGreaterThan(NOW.getTime());

    const slots = w.queue.map((i) => Date.parse(i.schedule_slot_at));
    expect(slots).toEqual([...slots].sort((a, b) => a - b));
  });

  it("is built from now, so it is always current", () => {
    const tomorrow = sampleWorkspace(new Date(NOW.getTime() + 24 * 60 * 60 * 1000));
    expect(Date.parse(tomorrow.queue[0].schedule_slot_at) - Date.parse(w.queue[0].schedule_slot_at)).toBe(
      24 * 60 * 60 * 1000,
    );
  });

  it("adds up: the cards show figures, never a withheld dash", () => {
    const posted = w.stats.posts_by_day.reduce((a, d) => a + d.count, 0);
    expect(w.stats.intents_by_state.posted).toBe(posted);
    expect(Object.values(w.stats.posted_by_source).reduce((a, n) => a + n, 0)).toBe(posted);

    const summary = deriveSummary(w.stats);
    expect(summary.success_rate).not.toBeNull();
    expect(summary.avg_per_day).not.toBeNull();

    const mix = deriveFolderMix(w.stats, w.mix);
    expect(mix.total).toBe(posted);
    expect(mix.fromRemoved).toBe(0);
    expect(mix.folders.map((f) => [f.name, f.planned])).toEqual([
      ["Product shots", 50],
      ["Behind the scenes", 30],
      ["Memes", 20],
    ]);
    for (const folder of mix.folders) expect(folder.posted).not.toBeNull();
  });
});
