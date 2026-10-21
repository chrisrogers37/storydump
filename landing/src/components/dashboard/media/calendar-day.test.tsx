/**
 * The calendar's day view draws each story's picture in a row-size box, the
 * Queue's (#1634), and lists the slots the cadence will open among
 * the stories, as predicted. Read as a returned element tree,
 * without a DOM.
 */

import { describe, expect, it } from "vitest";
import { isValidElement, type ReactElement } from "react";
import { ImageIcon } from "lucide-react";
import type { PredictedSlot, RepeatOccurrence } from "@/lib/calendar-month";
import type { Intent } from "@/lib/intents";
import { RepeatMark } from "@/components/dashboard/repeat-mark";
import { CalendarDay } from "./calendar-day";
import { MediaThumbnail } from "./media-thumbnail";
import { OpenedDayTitle } from "./opened-day";

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

/** The fields the day view reads; the rest of an intent is not its concern. */
const story = (id: string, over: Partial<Intent> = {}) =>
  ({
    id,
    state: "posted",
    schedule_slot_at: "2026-10-09T14:00:00Z",
    file_name: `${id}.jpg`,
    media_item_id: `media-${id}`,
    media_kind: "image",
    has_thumbnail: true,
    thumbnail_version: "v1",
    ...over,
  }) as Intent;

/** The text a tree draws: its strings and numbers, joined. */
function textOf(node: unknown): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join("");
  if (!isValidElement(node)) return "";
  return textOf((node.props as { children?: unknown }).children);
}

/** A slot the cadence will open on the day. */
const slot = (at: string, over: Partial<PredictedSlot> = {}): PredictedSlot => ({
  kind: "predicted",
  schedule_slot_at: at,
  day: "2026-10-09",
  tz: "America/New_York",
  ig_account_id: "a1",
  account_handle: "example.brand",
  account_display_name: "Example Co",
  ...over,
});

type DayProps = Parameters<typeof CalendarDay>[0];

const day = (
  intents: Intent[],
  predicted: PredictedSlot[] = [],
  predictedCut = false,
  over: Partial<DayProps> = {}
) =>
  CalendarDay({
    date: "2026-10-09",
    intents,
    predicted,
    repeating: [],
    tz: "UTC",
    workspaceId: "ws-1",
    closeHref: "?month=2026-10",
    truncatedAt: null,
    predictedCut,
    repeatingCut: false,
    ...over,
  }) as ReactElement;

/** The repeat marks a tree draws, by their props. */
const marks = (tree: ReactElement) =>
  [...walk(tree)]
    .filter((el) => el.type === RepeatMark)
    .map((el) => el.props as Parameters<typeof RepeatMark>[0]);

/**
 * The text of each of the day's rows, in order. ICU may put a narrow no-break
 * space before AM/PM, so whitespace is compared as words, not bytes.
 */
const rowsOf = (tree: ReactElement) =>
  [...walk(tree)].filter((el) => el.type === "li").map((li) => textOf(li).replace(/\s/g, " "));

describe("the day view's pictures (#1634)", () => {
  it("draws each story's picture through the route, row-size, as the Queue does", () => {
    const tree = day([story("a"), story("b", { media_kind: "video" })]);
    const pictures = [...walk(tree)].filter((el) => el.type === MediaThumbnail);
    expect(pictures.map((el) => (el.props as { src: string }).src)).toEqual([
      "/api/workspaces/ws-1/media/media-a/thumbnail?v=v1",
      "/api/workspaces/ws-1/media/media-b/thumbnail?v=v1",
    ]);
    expect(pictures.map((el) => (el.props as { video: boolean }).video)).toEqual([false, true]);
    const boxes = [...walk(tree)].filter((el) =>
      String((el.props as { className?: string }).className ?? "").includes("h-10 w-10"),
    );
    expect(boxes).toHaveLength(2);
  });

  it("draws the glyph for a story without a picture", () => {
    const tree = day([story("c", { has_thumbnail: false })]);
    const types = [...walk(tree)].map((el) => el.type);
    expect(types).not.toContain(MediaThumbnail);
    expect(types).toContain(ImageIcon);
  });
});

describe("the day view's repeating stories (#485)", () => {
  const rule = { unit: "day" as const, every: 2, days: null, at: "09:00" };

  const waiting = { state: "scheduled" } as const;

  it("marks a story that repeats, with its rule, and no other", () => {
    const tree = day([story("a", { ...waiting, repeat: rule }), story("b", waiting)]);
    expect(marks(tree).map((mark) => mark.rule)).toEqual([rule]);
  });

  it("marks only a story that lives: an ended one keeps its rule, but its series may have stopped", () => {
    const tree = day([story("a", { repeat: rule }), story("b", { ...waiting, repeat: rule })]);
    expect(marks(tree)).toHaveLength(1);
  });

  it("names the story's zone on its mark only where the page reads another", () => {
    const tree = day([
      story("a", { ...waiting, repeat: rule, tz: "Europe/London" }),
      story("b", { ...waiting, repeat: rule, tz: "UTC" }),
    ]);
    expect(marks(tree).map((mark) => mark.zone)).toEqual(["Europe/London", undefined]);
    const mark = RepeatMark({ rule, zone: "Europe/London" }) as ReactElement<{ title: string }>;
    expect(mark.props.title).toBe("Repeats every 2 days at 9:00 AM (Europe/London)");
  });
});

describe("the day view's later occurrences of a series (#485)", () => {
  const rule = { unit: "week" as const, every: 1, days: [1, 4], at: "16:07" };
  const later = (over: Partial<RepeatOccurrence> = {}): RepeatOccurrence => ({
    kind: "repeat",
    schedule_slot_at: "2026-10-09T20:07:00+00:00",
    day: "2026-10-09",
    tz: "UTC",
    intent_id: "series-1",
    media_item_id: "media-s",
    ig_account_id: "a1",
    file_name: "weekly.jpg",
    media_kind: "image",
    has_thumbnail: false,
    thumbnail_version: "v1",
    category: null,
    repeat: rule,
    ...over,
  });

  it("lists an occurrence as a place that opens its series in the Queue, with no state", () => {
    const tree = day([], [], false, { repeating: [later({ tz: "Asia/Tokyo" })] });
    const links = [...walk(tree)].filter(
      (el) =>
        (el.props as { href?: string }).href === "/dashboard/queue?origin=planned#story-media-s-a1"
    );
    expect(links).toHaveLength(1);
    expect(textOf(links[0])).toBe("weekly.jpg: open its series in the Queue");
    expect(marks(tree)).toEqual([{ rule, zone: "Asia/Tokyo" }]);
    expect(rowsOf(tree)).toHaveLength(1);
    expect(rowsOf(tree)[0]).toMatch(/repeats$/);
  });

  it("takes its place in time among the stories and the predicted slots", () => {
    const tree = day(
      [story("a", { schedule_slot_at: "2026-10-09T13:00:00Z" })],
      [slot("2026-10-09T16:00:00+00:00")],
      false,
      { repeating: [later()] }
    );
    expect(rowsOf(tree).map((row) => /^\d+:\d{2} [AP]M/.exec(row)?.[0])).toEqual([
      "1:00 PM",
      "4:00 PM",
      "8:07 PM",
    ]);
    expect(rowsOf(tree)[2]).toMatch(/weekly\.jpg/);
  });

  it("says when the occurrences were cut on or before the day", () => {
    const cut = day([], [], false, { repeating: [later()], repeatingCut: true });
    expect(textOf(cut)).toContain("This day may hold more repeats than are shown.");
    expect(textOf(day([], [], false, { repeating: [later()] }))).not.toContain("more repeats");
  });
});

describe("a past-posts story in the day view (#1644)", () => {
  const FAKE_ID = "17900000000000001";
  const GENERATION = "5b0e1c2e-8c67-4d29-9a3f-0c1d2e3f4a5b";
  const pastPosts = (id: string, over: Partial<Intent> = {}) =>
    story(id, {
      file_name: `Instagram ${FAKE_ID}`,
      thumbnail_version: `${GENERATION}.7`,
      provenance: {
        provider: "instagram_past_posts",
        origin_handle: "example_handle",
        original_at: "2020-01-01T12:00:00+00:00",
      },
      ...over,
    });

  it("goes by its own name, never the id it is stored under, beside a Drive story's", () => {
    const tree = day([pastPosts("a"), story("b")]);
    expect(rowsOf(tree)).toEqual(["2:00 PMInstagram postposted", "2:00 PMb.jpgposted"]);
    expect(JSON.stringify(textOf(tree))).not.toContain(FAKE_ID);
    const titles = [...walk(tree)]
      .map((el) => (el.props as { title?: string }).title)
      .filter(Boolean);
    expect(titles).toEqual(["Instagram post", "b.jpg"]);
  });

  it("asks for its picture under the past-posts source's limits, in the row's box; a Drive story's stays the browser's", () => {
    const tree = day([pastPosts("a"), story("b")]);
    const pictures = [...walk(tree)].filter((el) => el.type === MediaThumbnail);
    expect(
      pictures.map((el) => (el.props as { pastPosts?: unknown }).pastPosts),
    ).toEqual([{ workspaceId: "ws-1", layout: "tile" }, undefined]);
    expect((pictures[0].props as { src: string; alt: string }).src).toBe(
      `/api/workspaces/ws-1/media/media-a/thumbnail?v=${GENERATION}.7`,
    );
    expect((pictures[0].props as { alt: string }).alt).toBe("Instagram post");
  });
});

describe("the day view's predicted slots (#1634)", () => {
  it("lists each slot among the stories in time order, as predicted, in the workspace's zone", () => {
    const tree = day(
      [story("a", { state: "scheduled", schedule_slot_at: "2026-10-09T14:00:00.123456+00:00" })],
      [slot("2026-10-09T12:00:00+00:00"), slot("2026-10-09T16:00:00+00:00")],
    );
    expect(rowsOf(tree)).toEqual([
      "12:00 PM@example.brandpredicted",
      "2:00 PMa.jpgscheduled",
      "4:00 PM@example.brandpredicted",
    ]);
  });

  it("draws no picture for a slot, which has no file until it draws one", () => {
    const tree = day([], [slot("2026-10-09T12:00:00+00:00")]);
    const types = [...walk(tree)].map((el) => el.type);
    expect(types).not.toContain(MediaThumbnail);
    expect(types).not.toContain(ImageIcon);
  });

  it("puts a story before a slot at the same time", () => {
    const at = "2026-10-09T12:00:00+00:00";
    const tree = day([story("a", { state: "scheduled", schedule_slot_at: at })], [slot(at)]);
    expect(rowsOf(tree)).toEqual([
      "12:00 PMa.jpgscheduled",
      "12:00 PM@example.brandpredicted",
    ]);
  });

  it("names a slot's account by its handle, else by its name", () => {
    const tree = day(
      [],
      [
        slot("2026-10-09T12:00:00+00:00", { ig_account_id: "a2", account_handle: null }),
        slot("2026-10-09T13:00:00+00:00", {
          ig_account_id: "a3",
          account_handle: null,
          account_display_name: null,
        }),
      ],
    );
    expect(rowsOf(tree)).toEqual([
      "12:00 PMExample Copredicted",
      "1:00 PMAn accountpredicted",
    ]);
  });

  it("says when the day may hold more predicted slots than were read", () => {
    const cut = textOf(day([], [slot("2026-10-09T12:00:00+00:00")], true));
    expect(cut).toContain("This day may hold more predicted slots than are shown.");
    expect(textOf(day([], [slot("2026-10-09T12:00:00+00:00")]))).not.toContain("may hold more");
  });

  it("reads as empty only with no story and no slot", () => {
    expect(textOf(day([]))).toContain("No stories on this day.");
    expect(textOf(day([], [slot("2026-10-09T12:00:00+00:00")]))).not.toContain("No stories");
  });
});

describe("the day view on a phone", () => {
  // At 390 px the picture, the time and "awaiting approval" left a name 22 px:
  // "m…". A story's row and a predicted slot's are separate branches, so both are read.
  const classOf = (el: ReactElement) =>
    String((el.props as { className?: string }).className ?? "").split(/\s+/);
  const rows = () =>
    [...walk(day([story("a")], [slot("2026-10-09T16:00:00+00:00")]))].filter(
      (el) => el.type === "li",
    );
  /** Each row's name: the one piece of the row that grows. */
  const names = () =>
    rows().map((row) =>
      [...walk(row)]
        .filter((el) => el.type === "span")
        .map(classOf)
        .filter((classes) => classes.includes("grow")),
    );

  it("wraps a row, so its state drops below the name rather than squeezing it", () => {
    expect(rows()).toHaveLength(2);
    for (const row of rows()) expect(classOf(row)).toContain("flex-wrap");
  });

  it("cuts a name with an ellipsis from sm up, and never below it", () => {
    for (const found of names()) {
      expect(found).toHaveLength(1);
      expect(found[0]).toContain("sm:truncate");
      expect(found[0]).not.toContain("truncate");
    }
  });

  it("breaks a long name onto a second line rather than letting it run out of the row", () => {
    for (const found of names()) expect(found[0]).toContain("break-words");
  });
});

describe("an opened day is put in front of the visitor", () => {
  it("draws its date as the title that takes focus when the day opens", () => {
    const titles = [...walk(day([story("a")]))].filter((el) => el.type === OpenedDayTitle);
    expect(titles).toHaveLength(1);
    expect((titles[0].props as { date: string }).date).toBe("2026-10-09");
    expect(textOf(titles[0])).toBe("Friday, October 9");
  });

  it("keeps a margin from the edge of the page it is scrolled to", () => {
    const view = day([story("a")]);
    expect(String((view.props as { className?: string }).className).split(/\s+/)).toContain(
      "scroll-my-6",
    );
  });
});
