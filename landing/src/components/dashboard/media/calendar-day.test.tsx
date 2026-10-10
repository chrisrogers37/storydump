/**
 * The calendar's day view draws each story's picture in a row-size box, the
 * Queue's (#1634 Phase 4). Read as a returned element tree, without a DOM.
 */

import { describe, expect, it } from "vitest";
import { isValidElement, type ReactElement } from "react";
import { ImageIcon } from "lucide-react";
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

const day = (intents: Intent[]) =>
  CalendarDay({
    date: "2026-10-09",
    intents,
    tz: "UTC",
    workspaceId: "ws-1",
    closeHref: "?month=2026-10",
    truncatedAt: null,
  }) as ReactElement;

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
