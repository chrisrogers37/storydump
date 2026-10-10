/**
 * The calendar reads today and every item's day on the workspace's clock
 * (#1511). Before, it read the server's (UTC) or the browser's, so from the
 * evening in New York it highlighted tomorrow and drew an evening slot on the
 * next day. It draws the month it is given (#1634), and a day's posted count
 * is the month read's count, not the names it carried.
 */

import { describe, expect, it } from "vitest";
import { isValidElement, type ReactElement } from "react";
import { ImageIcon, Video } from "lucide-react";
import type { IntentDay } from "@/lib/calendar-month";
import { MediaThumbnail } from "./media-thumbnail";
import { buildCalendarDays, DayChips, listedDays } from "./content-calendar";

/** 9:30 PM on Thursday, Oct 1 in New York; already Friday, Oct 2 in UTC. */
const EVENING = new Date("2026-10-02T01:30:00Z");
const OCTOBER = { year: 2026, month: 10 };

const todays = (days: { date: string; isToday: boolean }[]) =>
  days.filter((d) => d.isToday).map((d) => d.date);

const postedDay = (date: string, count: number, names: string[]): IntentDay => ({
  date,
  count,
  newest: names.map((name, i) => ({
    id: `id-${name}`,
    state: "posted",
    schedule_slot_at: `${date}T${String(20 - i).padStart(2, "0")}:00:00Z`,
    file_name: name,
    category: "Memes",
  })),
});

describe("the calendar on the workspace's clock", () => {
  it("marks the workspace's today, not the UTC one", () => {
    expect(
      todays(buildCalendarDays(OCTOBER, [], [], [], "America/New_York", EVENING).days)
    ).toEqual(["2026-10-01"]);
    expect(todays(buildCalendarDays(OCTOBER, [], [], [], "UTC", EVENING).days)).toEqual([
      "2026-10-02",
    ]);
  });

  it("draws an evening slot on the workspace's day", () => {
    // 8:30 PM on Oct 1 in New York, 00:30 on Oct 2 in UTC.
    const slot = { slot_time: "2026-10-02T00:30:00Z", predicted_category: "Memes" };
    const { days } = buildCalendarDays(OCTOBER, [], [], [slot], "America/New_York", EVENING);
    expect(days.find((d) => d.date === "2026-10-01")!.posts).toHaveLength(1);
    expect(days.find((d) => d.date === "2026-10-02")!.posts).toHaveLength(0);
  });

  it("draws the month's posted names on the day the read grouped them, beside the queue", () => {
    const queued = {
      scheduled_for: "2026-10-02T03:00:00Z",
      media_name: "b.jpg",
      category: "Memes",
      status: "approved",
      planned: false,
    };
    const { days } = buildCalendarDays(
      OCTOBER,
      [postedDay("2026-10-01", 1, ["a.jpg"])],
      [queued],
      [],
      "America/New_York",
      EVENING
    );
    expect(days.find((d) => d.date === "2026-10-01")!.posts.map((p) => [p.label, p.type])).toEqual([
      ["a.jpg", "past"],
      ["b.jpg", "queued"],
    ]);
  });

  it("counts a busy day from the read's count, not from the names it carried (#1634)", () => {
    const { days } = buildCalendarDays(
      OCTOBER,
      [postedDay("2026-10-05", 15, ["c.jpg", "b.jpg", "a.jpg"])],
      [],
      [],
      "UTC",
      EVENING
    );
    const busy = days.find((d) => d.date === "2026-10-05")!;
    expect(busy.posted).toBe(15);
    expect(busy.postedUnnamed).toBe(12);
    expect(busy.posts).toHaveLength(3);
    const quiet = days.find((d) => d.date === "2026-10-06")!;
    expect([quiet.posted, quiet.postedUnnamed]).toEqual([0, 0]);
  });

  it("draws a story a person planned as planned, apart from the slot plan's (#1413)", () => {
    const at = "2026-10-01T14:00:00Z";
    const planned = { scheduled_for: at, media_name: "p.jpg", category: "Memes", status: "scheduled", planned: true };
    const queued = { scheduled_for: at, media_name: "q.jpg", category: "Memes", status: "approved", planned: false };
    const { days } = buildCalendarDays(OCTOBER, [], [planned, queued], [], "UTC", EVENING);
    expect(days.find((d) => d.date === "2026-10-01")!.posts.map((p) => [p.label, p.type])).toEqual([
      ["p.jpg", "planned"],
      ["q.jpg", "queued"],
    ]);
  });

  it("shows the month it is given, in whole Monday-first weeks", () => {
    // 10 PM on Saturday, Oct 31 in New York; already November in UTC.
    const { month, days } = buildCalendarDays(OCTOBER, [], [], [], "America/New_York", new Date("2026-11-01T02:00:00Z"));
    expect(month).toBe("October 2026");
    expect(days[0].date).toBe("2026-09-28");
    expect(days.at(-1)!.date).toBe("2026-11-01");
    expect(days.length % 7).toBe(0);
    expect(days.filter((d) => d.isCurrentMonth)).toHaveLength(31);
    expect(todays(days)).toEqual(["2026-10-31"]);
  });

  it("draws another month than today's, for the month navigation (#1634)", () => {
    const { month, days } = buildCalendarDays({ year: 2026, month: 9 }, [], [], [], "UTC", EVENING);
    expect(month).toBe("September 2026");
    expect(days[0].date).toBe("2026-08-31");
    expect(days.filter((d) => d.isCurrentMonth)).toHaveLength(30);
  });
  it("lists a phone's days: this month's that hold something, in order (#1649 F12)", () => {
    const queued = (at: string, name: string) => ({
      scheduled_for: at,
      media_name: name,
      category: "Memes",
      status: "approved",
      planned: false,
    });
    const { days } = buildCalendarDays(
      OCTOBER,
      [postedDay("2026-10-05", 15, ["c.jpg"])],
      // Nov 1 is on October's grid, but it is not October's to list.
      [queued("2026-10-01T14:00:00Z", "q.jpg"), queued("2026-11-01T14:00:00Z", "n.jpg")],
      [],
      "UTC",
      EVENING
    );
    expect(listedDays(days).map((d) => d.date)).toEqual(["2026-10-01", "2026-10-05"]);
  });
});

/** Every element in a returned tree, depth-first. */
function* walk(node: unknown): Generator<ReactElement> {
  if (Array.isArray(node)) {
    for (const child of node) yield* walk(child);
    return;
  }
  if (!isValidElement(node)) return;
  yield node;
  yield* walk((node.props as { children?: unknown }).children);
}

describe("the calendar's pictures (#1634 Phase 4)", () => {
  const pictured: IntentDay = {
    date: "2026-10-01",
    count: 1,
    newest: [
      {
        ...postedDay("2026-10-01", 1, ["a.jpg"]).newest[0],
        media_item_id: "media-a",
        media_kind: "image",
        has_thumbnail: true,
        thumbnail_version: "v1",
      },
    ],
  };
  const queuedClip = {
    scheduled_for: "2026-10-02T14:00:00Z",
    media_name: "q.mp4",
    category: "Memes",
    status: "approved",
    planned: false,
    media: {
      file_name: "q.mp4",
      media_item_id: "media-q",
      media_kind: "video",
      has_thumbnail: true,
      thumbnail_version: "v2",
    },
  };
  // The sample workspace's queue carries no picture fields.
  const sampleQueued = { ...queuedClip, media_name: "s.jpg", media: undefined };
  const slot = { slot_time: "2026-10-02T16:00:00Z", predicted_category: "Memes" };
  const october = () =>
    buildCalendarDays(OCTOBER, [pictured], [queuedClip, sampleQueued], [slot], "UTC", EVENING).days;
  const dayOf = (date: string) => october().find((d) => d.date === date)!;

  it("gives each story chip its picture's fields, and a predicted slot none", () => {
    expect(dayOf("2026-10-01").posts[0].media).toMatchObject({
      media_item_id: "media-a",
      has_thumbnail: true,
    });
    expect(
      dayOf("2026-10-02").posts.map((p) => [p.label, p.media?.media_item_id, p.media?.file_name])
    ).toEqual([
      ["q.mp4", "media-q", "q.mp4"],
      ["s.jpg", undefined, "s.jpg"],
      ["Memes", undefined, undefined],
    ]);
  });

  it("draws a picture beside a chip's name through the route, with no play badge", () => {
    const tree = DayChips({ day: dayOf("2026-10-02"), size: "cell", workspaceId: "ws-1" }) as ReactElement;
    const pictures = [...walk(tree)].filter((el) => el.type === MediaThumbnail);
    expect(pictures.map((el) => (el.props as { src: string }).src)).toEqual([
      "/api/workspaces/ws-1/media/media-q/thumbnail?v=v2",
    ]);
    expect((pictures[0].props as { video: boolean }).video).toBe(false);
    // The sample's story draws its glyph; the predicted slot draws none.
    expect([...walk(tree)].filter((el) => el.type === ImageIcon)).toHaveLength(1);
  });

  it("draws every story chip's glyph without a workspace, as the sample workspace does", () => {
    const tree = DayChips({ day: dayOf("2026-10-02"), size: "row", workspaceId: null }) as ReactElement;
    const types = [...walk(tree)].map((el) => el.type);
    expect(types).not.toContain(MediaThumbnail);
    expect(types.filter((type) => type === Video || type === ImageIcon)).toEqual([Video, ImageIcon]);
  });
});
