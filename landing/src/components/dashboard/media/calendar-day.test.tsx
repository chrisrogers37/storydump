/**
 * The calendar's day view draws each story's picture in a row-size box, the
 * Queue's (#1634 Phase 4), and lists the slots the cadence will open among
 * the stories, as predicted (Phase 3b). Read as a returned element tree,
 * without a DOM.
 */

import { describe, expect, it } from "vitest";
import { isValidElement, type ReactElement } from "react";
import { ImageIcon } from "lucide-react";
import type { PredictedSlot } from "@/lib/calendar-month";
import type { Intent } from "@/lib/intents";
import { CalendarDay } from "./calendar-day";
import { MediaThumbnail } from "./media-thumbnail";

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

const day = (intents: Intent[], predicted: PredictedSlot[] = [], predictedCut = false) =>
  CalendarDay({
    date: "2026-10-09",
    intents,
    predicted,
    tz: "UTC",
    workspaceId: "ws-1",
    closeHref: "?month=2026-10",
    truncatedAt: null,
    predictedCut,
  }) as ReactElement;

/**
 * The text of each of the day's rows, in order. ICU may put a narrow no-break
 * space before AM/PM, so whitespace is compared as words, not bytes.
 */
const rowsOf = (tree: ReactElement) =>
  [...walk(tree)].filter((el) => el.type === "li").map((li) => textOf(li).replace(/\s/g, " "));

describe("the day view's pictures (#1634 Phase 4)", () => {
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

describe("the day view's predicted slots (#1634 Phase 3b)", () => {
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
