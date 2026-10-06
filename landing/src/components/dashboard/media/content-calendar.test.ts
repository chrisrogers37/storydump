/**
 * The calendar reads today, its month and every item's day on the
 * workspace's clock (#1511). Before, it read the server's (UTC) or the
 * browser's, so from the evening in New York it highlighted tomorrow and
 * drew an evening slot on the next day.
 */

import { describe, expect, it } from "vitest";
import { buildCalendarDays } from "./content-calendar";

/** 9:30 PM on Thursday, Oct 1 in New York; already Friday, Oct 2 in UTC. */
const EVENING = new Date("2026-10-02T01:30:00Z");

const todays = (days: { date: string; isToday: boolean }[]) =>
  days.filter((d) => d.isToday).map((d) => d.date);

describe("the calendar on the workspace's clock", () => {
  it("marks the workspace's today, not the UTC one", () => {
    expect(todays(buildCalendarDays([], [], [], "America/New_York", EVENING).days)).toEqual([
      "2026-10-01",
    ]);
    expect(todays(buildCalendarDays([], [], [], "UTC", EVENING).days)).toEqual(["2026-10-02"]);
  });

  it("draws an evening slot on the workspace's day", () => {
    // 8:30 PM on Oct 1 in New York, 00:30 on Oct 2 in UTC.
    const slot = { slot_time: "2026-10-02T00:30:00Z", predicted_category: "Memes" };
    const { days } = buildCalendarDays([], [], [slot], "America/New_York", EVENING);
    expect(days.find((d) => d.date === "2026-10-01")!.posts).toHaveLength(1);
    expect(days.find((d) => d.date === "2026-10-02")!.posts).toHaveLength(0);
  });

  it("places posted and queued stories by the workspace's day too", () => {
    const posted = { posted_at: "2026-10-01T23:45:00Z", media_name: "a.jpg", category: "Memes", status: "posted" };
    const queued = { scheduled_for: "2026-10-02T03:00:00Z", media_name: "b.jpg", category: "Memes", status: "approved", planned: false };
    const { days } = buildCalendarDays([posted], [queued], [], "America/New_York", EVENING);
    expect(days.find((d) => d.date === "2026-10-01")!.posts.map((p) => p.label)).toEqual([
      "a.jpg",
      "b.jpg",
    ]);
  });

  it("draws a story a person planned as planned, apart from the slot plan's (#1413)", () => {
    const at = "2026-10-01T14:00:00Z";
    const planned = { scheduled_for: at, media_name: "p.jpg", category: "Memes", status: "scheduled", planned: true };
    const queued = { scheduled_for: at, media_name: "q.jpg", category: "Memes", status: "approved", planned: false };
    const { days } = buildCalendarDays([], [planned, queued], [], "UTC", EVENING);
    expect(days.find((d) => d.date === "2026-10-01")!.posts.map((p) => [p.label, p.type])).toEqual([
      ["p.jpg", "planned"],
      ["q.jpg", "queued"],
    ]);
  });

  it("shows the workspace's month, in whole Monday-first weeks", () => {
    // 10 PM on Saturday, Oct 31 in New York; already November in UTC.
    const { month, days } = buildCalendarDays([], [], [], "America/New_York", new Date("2026-11-01T02:00:00Z"));
    expect(month).toBe("October 2026");
    expect(days[0].date).toBe("2026-09-28");
    expect(days.at(-1)!.date).toBe("2026-11-01");
    expect(days.length % 7).toBe(0);
    expect(days.filter((d) => d.isCurrentMonth)).toHaveLength(31);
    expect(todays(days)).toEqual(["2026-10-31"]);
  });
});
