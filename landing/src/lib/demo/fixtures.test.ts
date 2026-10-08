import { describe, expect, it } from "vitest";
import { deriveConditions } from "@/lib/conditions";
import { deriveFolderMix, deriveSummary } from "@/lib/dashboard-payloads";
import { SAMPLE_POSTING_HOURS, SAMPLE_TZ, sampleWorkspace } from "./fixtures";

/** 17:20 in London: the six most recent slots run from 19:00 yesterday to 17:00 today. */
const NOW = new Date("2026-10-15T16:20:00.000Z");
const w = sampleWorkspace(NOW);
const stories = w.queue;

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

  it("posts only inside its posting hours, on its own clock, across both clock changes", () => {
    // Mid-October; the morning after London's clocks go back (25 October); the
    // morning after they go forward (29 March). Each month's window spans its change.
    for (const at of ["2026-10-15T16:20:00Z", "2026-10-26T10:00:00Z", "2026-03-30T09:00:00Z"]) {
      const sample = sampleWorkspace(new Date(at));
      for (const story of [...sample.queue, ...sample.history]) {
        const { hour, minute } = onItsClock(story.schedule_slot_at);
        expect(SAMPLE_POSTING_HOURS, `${at}: ${story.file_name}`).toContain(hour);
        expect(minute, `${at}: ${story.file_name}`).toBe(0);
      }
    }
  });

  it("counts its days by its own calendar, as `local_date` does", () => {
    // Half past midnight in London is still the evening before in UTC.
    const night = sampleWorkspace(new Date("2026-10-15T23:30:00Z"));
    expect(night.stats.posts_by_day.at(-1)?.local_date).toBe("2026-10-16");
    expect(w.stats.posts_by_day.at(-1)?.local_date).toBe(onItsClock(NOW.toISOString()).date);
  });

  it("draws every screen from one set of stories: each day's posts are the stories that posted that day", () => {
    const posted = w.history.filter((s) => s.state === "posted");
    for (const day of w.stats.posts_by_day) {
      const that = posted.filter((s) => onItsClock(s.entered_state_at).date === day.local_date);
      expect(day.count, day.local_date).toBe(that.length);
      expect(day.count).toBeLessThanOrEqual(day.cap);
    }
    const ended = (state: string) => w.history.filter((s) => s.state === state).length;
    expect(w.stats.intents_by_state.skipped).toBe(ended("skipped"));
    expect(w.stats.intents_by_state.rejected).toBe(ended("rejected"));
  });

  it("posts nothing today while today's slots wait for a tap", () => {
    // The chart once drew five posts on a day whose every slot was still in the Queue (#1649).
    const today = w.stats.posts_by_day.at(-1)!;
    const waitingToday = w.queue.filter(
      (i) => i.state === "awaiting_approval" && onItsClock(i.schedule_slot_at).date === today.local_date,
    );
    expect(waitingToday).toHaveLength(5);
    expect(today.count).toBe(0);
  });

  it("keeps a slot's story from one render to the next, and repeats no file in the window", () => {
    const later = sampleWorkspace(new Date(NOW.getTime() + 2 * 60 * 60 * 1000));
    const before = new Map(w.history.map((s) => [s.id, s] as const));
    const shared = later.history.filter((s) => before.has(s.id));
    expect(shared.length).toBeGreaterThan(100);
    for (const s of shared) expect(s).toEqual(before.get(s.id));
    expect(new Set(w.history.map((s) => s.file_name)).size).toBe(w.history.length);
  });

  it("posts by hand, as a new workspace does: no direct posting, so no failed publish", () => {
    expect(w.config.api_publishing_enabled).toBe(false);
    expect(w.stats.intents_by_state.failed).toBe(0);
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
