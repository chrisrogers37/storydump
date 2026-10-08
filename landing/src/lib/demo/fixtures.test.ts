import { describe, expect, it } from "vitest";
import { deriveConditions } from "@/lib/conditions";
import { deriveFolderMix, deriveSummary } from "@/lib/dashboard-payloads";
import { SAMPLE_POSTING_HOURS, SAMPLE_TZ, sampleWorkspace } from "./fixtures";

const NOW = new Date("2026-10-15T16:20:00.000Z");
const w = sampleWorkspace(NOW);
const stories = [...w.queue, ...w.history];

/** An instant on the sample workspace's own clock. */
function onItsClock(iso: string) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: SAMPLE_TZ,
    hourCycle: "h23",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "numeric",
    minute: "numeric",
  }).formatToParts(new Date(iso));
  const part = (type: string) => parts.find((p) => p.type === type)?.value ?? "";
  return {
    date: `${part("year")}-${part("month")}-${part("day")}`,
    hour: Number(part("hour")),
    minute: Number(part("minute")),
  };
}

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
      // No media, so the sample never asks the API for a thumbnail.
      expect(story.has_thumbnail).toBe(false);
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

  it("posts only inside its posting hours, on its own clock, across both clock changes", () => {
    // Mid-October; the morning after clocks go back; the morning after they go forward.
    for (const at of ["2026-10-15T16:20:00Z", "2026-11-01T14:00:00Z", "2026-03-08T15:00:00Z"]) {
      const sample = sampleWorkspace(new Date(at));
      for (const story of [...sample.queue, ...sample.history]) {
        const { hour, minute } = onItsClock(story.schedule_slot_at);
        expect(SAMPLE_POSTING_HOURS, `${at}: ${story.file_name}`).toContain(hour);
        expect(minute, `${at}: ${story.file_name}`).toBe(0);
      }
    }
  });

  it("counts its days by its own calendar, as `local_date` does", () => {
    // 22:00 in New York is already the next day in UTC.
    const evening = sampleWorkspace(new Date("2026-10-16T02:00:00Z"));
    expect(evening.stats.posts_by_day.at(-1)?.local_date).toBe("2026-10-15");
    expect(w.stats.posts_by_day.at(-1)?.local_date).toBe(onItsClock(NOW.toISOString()).date);
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
