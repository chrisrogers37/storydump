/**
 * The calendar reads today and every item's day on the workspace's clock
 * (#1511), never the server's (UTC) or the browser's: on an evening in New
 * York it highlights that evening's date and draws an evening slot on it. It
 * draws the month it is given (#1634), and a day's posted count
 * is the month read's count, not the names it carried. A day ahead holds the
 * stories a person planned and a count of the slots the cadence will open,
 * labelled as predicted, and says when a list of them was cut.
 */

import { describe, expect, it } from "vitest";
import { createElement, isValidElement, type ReactElement } from "react";
import { renderToString } from "react-dom/server";
import { ImageIcon, Video } from "lucide-react";
import type { IntentDay } from "@/lib/calendar-month";
import { TONE_CLASS } from "@/components/dashboard/tone";
import { RepeatMark } from "@/components/dashboard/repeat-mark";
import { MediaThumbnail } from "./media-thumbnail";
import {
  buildCalendarDays,
  ContentCalendar,
  dayLabel,
  DayChips,
  listedDays,
} from "./content-calendar";

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
    category: "Events",
  })),
});

describe("the calendar on the workspace's clock", () => {
  it("keeps the slide name returned by the server on a posted day", () => {
    const { days } = buildCalendarDays(OCTOBER, [postedDay("2026-10-01", 1, ["Instagram post, slide 3 of 10"])], [], [], "UTC", EVENING);
    expect(days.find((day) => day.date === "2026-10-01")?.posts[0].label).toBe("Instagram post, slide 3 of 10");
  });
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
    const queued = {
      scheduled_for: "2026-10-02T00:30:00Z",
      media_name: "q.jpg",
      category: "Events",
      type: "queued" as const,
    };
    const { days } = buildCalendarDays(OCTOBER, [], [queued], [], "America/New_York", EVENING);
    expect(days.find((d) => d.date === "2026-10-01")!.posts).toHaveLength(1);
    expect(days.find((d) => d.date === "2026-10-02")!.posts).toHaveLength(0);
  });

  it("draws the month's posted names on the day the read grouped them, beside the queue", () => {
    const queued = {
      scheduled_for: "2026-10-02T03:00:00Z",
      media_name: "b.jpg",
      category: "Events",
      type: "queued" as const,
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
    const planned = { scheduled_for: at, media_name: "p.jpg", category: "Events", type: "planned" as const };
    const queued = { scheduled_for: at, media_name: "q.jpg", category: "Events", type: "queued" as const };
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
  it("lists a phone's days: this month's that hold something, in order (#1649)", () => {
    const queued = (at: string, name: string) => ({
      scheduled_for: at,
      media_name: name,
      category: "Events",
      type: "queued" as const,
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

/** The text a tree draws: its strings and numbers, joined. */
function textOf(node: unknown): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join("");
  if (!isValidElement(node)) return "";
  return textOf((node.props as { children?: unknown }).children);
}

const classOf = (el: ReactElement) => String((el.props as { className?: string }).className ?? "");

/** A story a person planned, on the day the read placed it on. */
const planned = (at: string, day: string, name: string) => ({
  scheduled_for: at,
  day,
  media_name: name,
  category: "Events",
  type: "planned" as const,
});

describe("the calendar's days ahead (#1634)", () => {
  it("draws a planned story on the day the read placed it on", () => {
    // 8:30 PM on Oct 1 in New York, already Oct 2 in UTC: the read placed it on the 1st.
    const { days } = buildCalendarDays(
      OCTOBER,
      [],
      [planned("2026-10-02T00:30:00Z", "2026-10-01", "p.jpg")],
      [],
      "UTC",
      EVENING
    );
    expect(days.find((d) => d.date === "2026-10-01")!.posts.map((p) => [p.label, p.type])).toEqual([
      ["p.jpg", "planned"],
    ]);
    expect(days.find((d) => d.date === "2026-10-02")!.posts).toEqual([]);
  });

  it("counts a day's predicted slots and never draws one as a story", () => {
    const { days } = buildCalendarDays(OCTOBER, [], [], [{ date: "2026-10-20", count: 6 }], "UTC", EVENING);
    const ahead = days.find((d) => d.date === "2026-10-20")!;
    expect([ahead.predicted, ahead.posts]).toEqual([6, []]);
    expect(days.filter((d) => d.predicted > 0).map((d) => d.date)).toEqual(["2026-10-20"]);
  });

  it("labels the count as predicted, in the legend's predicted tone, with no picture", () => {
    const { days } = buildCalendarDays(
      OCTOBER,
      [],
      [planned("2026-10-20T13:00:00Z", "2026-10-20", "p.jpg")],
      [{ date: "2026-10-20", count: 6 }],
      "UTC",
      EVENING
    );
    const tree = DayChips({ day: days.find((d) => d.date === "2026-10-20")!, size: "cell", workspaceId: "ws-1" }) as ReactElement;
    const count = [...walk(tree)].filter((el) => textOf(el) === "6 predicted");
    expect(count).toHaveLength(1);
    for (const token of TONE_CLASS.inert.split(" ")) expect(classOf(count[0]).split(" ")).toContain(token);
    // One glyph, the planned story's: the count draws no picture.
    expect([...walk(tree)].filter((el) => el.type === ImageIcon)).toHaveLength(1);
  });

  it("says a day from the first cut day on may hold more than it shows", () => {
    const { days } = buildCalendarDays(OCTOBER, [], [], [], "UTC", EVENING, "2026-10-28");
    expect(days.filter((d) => d.incomplete).map((d) => d.date)).toEqual([
      "2026-10-28",
      "2026-10-29",
      "2026-10-30",
      "2026-10-31",
      "2026-11-01",
    ]);
    const cut = DayChips({ day: days.find((d) => d.date === "2026-10-28")!, size: "row", workspaceId: null });
    expect(textOf(cut)).toBe("Not all shown");
    const whole = DayChips({ day: days.find((d) => d.date === "2026-10-27")!, size: "row", workspaceId: null });
    expect(textOf(whole)).toBe("");
  });

  it("lists on a phone a day that holds only predicted slots, or may hold more", () => {
    const { days } = buildCalendarDays(OCTOBER, [], [], [{ date: "2026-10-20", count: 2 }], "UTC", EVENING, "2026-10-30");
    expect(listedDays(days).map((d) => d.date)).toEqual(["2026-10-20", "2026-10-30", "2026-10-31"]);
  });

  it("tells a screen reader everything a day draws, not 0 posted for a day ahead", () => {
    const { days } = buildCalendarDays(
      OCTOBER,
      [postedDay("2026-10-01", 15, ["a.jpg"])],
      [planned("2026-10-20T13:00:00Z", "2026-10-20", "p.jpg")],
      [{ date: "2026-10-20", count: 6 }],
      "UTC",
      EVENING,
      "2026-10-20"
    );
    const label = (date: string) => dayLabel(days.find((d) => d.date === date)!);
    expect(label("2026-10-01")).toBe("Thursday, October 1: 15 posted");
    expect(label("2026-10-20")).toBe("Tuesday, October 20: 1 planned, 6 predicted, not all shown");
    expect(label("2026-10-05")).toBe("Monday, October 5");
  });
});

describe("a series' later occurrences (#485)", () => {
  const RULE = { unit: "week" as const, every: 1, days: [1, 4], at: "16:07" };
  const later = (at: string, day: string, zone?: string) => ({
    ...planned(at, day, "w.jpg"),
    type: "repeat" as const,
    rule: RULE,
    zone,
  });

  it("draws a later occurrence as a repeat, apart from its waiting story", () => {
    const { days } = buildCalendarDays(
      OCTOBER,
      [],
      [
        planned("2026-10-15T20:07:00Z", "2026-10-15", "w.jpg"),
        later("2026-10-19T20:07:00Z", "2026-10-19"),
      ],
      [],
      "UTC",
      EVENING
    );
    expect(days.find((d) => d.date === "2026-10-15")!.posts.map((p) => [p.label, p.type])).toEqual([
      ["w.jpg", "planned"],
    ]);
    const monday = days.find((d) => d.date === "2026-10-19")!;
    expect(monday.posts).toMatchObject([{ label: "w.jpg", type: "repeat", rule: RULE }]);
    expect(dayLabel(monday)).toBe("Monday, October 19: 1 repeating");
    expect(listedDays(days).map((d) => d.date)).toEqual(["2026-10-15", "2026-10-19"]);
  });

  it("counts a day's occurrences past the ones it names into its +N more", () => {
    const { days } = buildCalendarDays(
      OCTOBER,
      [],
      [later("2026-10-19T13:00:00Z", "2026-10-19"), later("2026-10-19T14:00:00Z", "2026-10-19")],
      [],
      "UTC",
      EVENING,
      null,
      [{ date: "2026-10-19", count: 5 }]
    );
    const monday = days.find((d) => d.date === "2026-10-19")!;
    expect(monday.repeatsUnnamed).toBe(3);
    expect(dayLabel(monday)).toBe("Monday, October 19: 5 repeating");
    expect(textOf(DayChips({ day: monday, size: "row", workspaceId: null }))).toContain("+3 more");
  });

  it("draws its chip as the planned outline, dashed, with the repeat mark and its zone", () => {
    const { days } = buildCalendarDays(
      OCTOBER,
      [],
      [later("2026-10-19T20:07:00Z", "2026-10-19", "Asia/Tokyo")],
      [],
      "UTC",
      EVENING
    );
    const tree = DayChips({ day: days.find((d) => d.date === "2026-10-19")!, size: "cell", workspaceId: null }) as ReactElement;
    const chip = [...walk(tree)].find((el) => (el.props as { title?: string }).title === "w.jpg")!;
    expect(classOf(chip).split(" ")).toEqual(expect.arrayContaining(["border-dashed", "border-tap"]));
    expect([...walk(tree)].filter((el) => el.type === RepeatMark).map((el) => el.props)).toEqual([
      { rule: RULE, zone: "Asia/Tokyo", icon: "h-2.5 w-2.5" },
    ]);
  });

  it("says what the dashed outline means in the legend", () => {
    const html = renderToString(
      createElement(ContentCalendar, { month: OCTOBER, history: [], queue: [], predicted: [] })
    );
    expect(html).toContain("Repeats");
  });
});

describe("the calendar's pictures (#1634)", () => {
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
    category: "Events",
    type: "queued" as const,
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
  const predicted = [{ date: "2026-10-02", count: 1 }];
  const october = () =>
    buildCalendarDays(OCTOBER, [pictured], [queuedClip, sampleQueued], predicted, "UTC", EVENING).days;
  const dayOf = (date: string) => october().find((d) => d.date === date)!;

  it("gives each story chip its picture's fields; a predicted slot is a count, not a chip", () => {
    expect(dayOf("2026-10-01").posts[0].media).toMatchObject({
      media_item_id: "media-a",
      has_thumbnail: true,
    });
    expect(
      dayOf("2026-10-02").posts.map((p) => [p.label, p.media.media_item_id, p.media.file_name])
    ).toEqual([
      ["q.mp4", "media-q", "q.mp4"],
      ["s.jpg", undefined, "s.jpg"],
    ]);
    expect(dayOf("2026-10-02").predicted).toBe(1);
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

describe("an archive story on the calendar (#1644)", () => {
  const FAKE_ID = "17900000000000001";
  const provenance = {
    provider: "instagram_archive",
    origin_handle: "example_handle",
    original_at: "2020-01-01T12:00:00+00:00",
  };
  const pictured = (over: Record<string, unknown>) => ({
    media_item_id: "media-ig",
    media_kind: "image",
    has_thumbnail: true,
    thumbnail_version: "gen.7",
    ...over,
  });
  /** The month read's newest, one archive story and one Drive story. */
  const history: IntentDay = {
    date: "2026-10-01",
    count: 2,
    newest: [
      {
        ...postedDay("2026-10-01", 2, [`Instagram ${FAKE_ID}`, "d.jpg"]).newest[0],
        id: "story-archive",
        ...pictured({ provenance }),
      },
      {
        ...postedDay("2026-10-01", 2, [`Instagram ${FAKE_ID}`, "d.jpg"]).newest[1],
        ...pictured({ media_item_id: "media-d", thumbnail_version: "v1", provenance: null }),
      },
    ],
  };
  const october = () => buildCalendarDays(OCTOBER, [history], [], [], "UTC", EVENING).days;
  const day = () => october().find((d) => d.date === "2026-10-01")!;

  it("goes by its own name on the chip and in the day's link, never by the id it is stored under", () => {
    expect(day().posts.map((p) => p.label)).toEqual(["Instagram post", "d.jpg"]);
    expect(day().posts.map((p) => p.media.file_name)).toEqual(["Instagram post", "d.jpg"]);
    expect(JSON.stringify(day())).not.toContain(FAKE_ID);
  });

  it("draws a chip's glyph and asks for nothing, as a chip is too small for the placeholder and sits in a link", () => {
    const tree = DayChips({ day: day(), size: "cell", workspaceId: "ws-1" }) as ReactElement;
    const pictures = [...walk(tree)].filter((el) => el.type === MediaThumbnail);
    // The Drive chip's picture only; the archive chip draws the glyph.
    expect(pictures.map((el) => (el.props as { src: string }).src)).toEqual([
      "/api/workspaces/ws-1/media/media-d/thumbnail?v=v1",
    ]);
    expect([...walk(tree)].filter((el) => el.type === ImageIcon)).toHaveLength(1);
    // No chip holds a button: the day's cell is a link.
    expect([...walk(tree)].some((el) => el.type === "button")).toBe(false);
  });

  it("takes the archive path for a story as the API serves it: already named, told by its provenance alone", () => {
    // The API names an archive item itself and sends no Graph id; the chip is told from a
    // Drive chip by the provenance on the month read's row.
    const served: IntentDay = {
      ...history,
      newest: [{ ...history.newest[0], file_name: "Instagram post" }, history.newest[1]],
    };
    const days = buildCalendarDays(OCTOBER, [served], [], [], "UTC", EVENING).days;
    const first = days.find((d) => d.date === "2026-10-01")!;
    expect(first.posts.map((p) => p.label)).toEqual(["Instagram post", "d.jpg"]);
    const tree = DayChips({ day: first, size: "cell", workspaceId: "ws-1" }) as ReactElement;
    const pictures = [...walk(tree)].filter((el) => el.type === MediaThumbnail);
    expect(pictures.map((el) => (el.props as { src: string }).src)).toEqual([
      "/api/workspaces/ws-1/media/media-d/thumbnail?v=v1",
    ]);
    expect([...walk(tree)].filter((el) => el.type === ImageIcon)).toHaveLength(1);
  });

  it("draws it the same in a phone's row", () => {
    const tree = DayChips({ day: day(), size: "row", workspaceId: "ws-1" }) as ReactElement;
    expect([...walk(tree)].filter((el) => el.type === MediaThumbnail)).toHaveLength(1);
  });
});

describe("the calendar's links", () => {
  /** An empty October, rendered to a string: the links are all there is to read. */
  const draw = (props: { navigable?: boolean; monthLinks?: boolean; selected?: string }) =>
    renderToString(
      createElement(ContentCalendar, {
        month: { year: 2026, month: 10 },
        history: [],
        queue: [],
        predicted: [],
        ...props,
      }),
    );
  const DAY = 'href="?month=2026-10&amp;day=2026-10-10"';

  it("links each day and the months either side when it can be navigated", () => {
    const html = draw({ navigable: true });
    expect(html).toContain(DAY);
    expect(html).toContain("Previous month");
    expect(html).toContain("Next month");
  });

  it("can link its days alone, for a calendar that draws one month", () => {
    const html = draw({ navigable: true, monthLinks: false });
    expect(html).toContain(DAY);
    expect(html).not.toContain("Previous month");
    expect(html).not.toContain("Next month");
  });

  it("links nothing when it cannot be navigated", () => {
    const html = draw({});
    expect(html).not.toContain("day=");
    expect(html).not.toContain("Previous month");
  });

  it("marks the link of the day whose view is open, which the day view returns the visitor to", () => {
    const marked = (html: string) => html.match(/<a[^>]*aria-current="date"[^>]*>/g) ?? [];
    expect(marked(draw({ navigable: true }))).toEqual([]);
    // An empty month lists no day for phones, so the grid's cell is the one link.
    const open = marked(draw({ navigable: true, selected: "2026-10-10" }));
    expect(open).toHaveLength(1);
    expect(open[0]).toContain(DAY);
  });
});
