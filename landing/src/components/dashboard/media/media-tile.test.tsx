/**
 * The picture box the Queue's rows, the calendar's chips and its day view share
 * (#1634), read as a returned element tree, without a DOM.
 */

import { describe, expect, it } from "vitest";
import { isValidElement, type ReactElement } from "react";
import { ImageIcon, Video } from "lucide-react";
import { MediaThumbnail } from "./media-thumbnail";
import { mediaTile } from "./media-tile";

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

type ThumbProps = Parameters<typeof MediaThumbnail>[0];
const thumbnails = (tree: ReactElement) =>
  [...walk(tree)].filter((el) => el.type === MediaThumbnail) as ReactElement<ThumbProps>[];
const types = (tree: ReactElement) => [...walk(tree)].map((el) => el.type);

const clip = {
  file_name: "clip.mp4",
  media_kind: "video",
  media_item_id: "media-1",
  has_thumbnail: true,
  thumbnail_version: "v1",
};

describe("mediaTile", () => {
  it("draws the picture through the route in the caller's box, the glyph as its fallback", () => {
    const tree = mediaTile({ media: clip, workspaceId: "ws-1", box: "h-10 w-10 rounded-md", glyph: "h-5 w-5" });
    expect((tree.props as { className: string }).className).toContain("h-10 w-10 rounded-md");
    const [thumb] = thumbnails(tree);
    expect(thumb.props.src).toBe("/api/workspaces/ws-1/media/media-1/thumbnail?v=v1");
    expect(thumb.props.alt).toBe("clip.mp4");
    expect(thumb.props.video).toBe(true);
    expect((thumb.props.fallback as ReactElement).type).toBe(Video);
  });

  it("leaves a chip's video without the play badge, which is too big for it", () => {
    const [thumb] = thumbnails(
      mediaTile({ media: clip, workspaceId: "ws-1", box: "h-4 w-4", glyph: "h-3 w-3", badge: false }),
    );
    expect(thumb.props.video).toBe(false);
  });

  it("draws the glyph, and asks for nothing, without a picture, a workspace or the fields", () => {
    const glyphOnly = [
      mediaTile({ media: { ...clip, has_thumbnail: false }, workspaceId: "ws-1", box: "", glyph: "" }),
      mediaTile({ media: clip, workspaceId: null, box: "", glyph: "" }),
      mediaTile({ media: { file_name: "a.jpg" }, workspaceId: "ws-1", box: "", glyph: "" }),
    ];
    for (const tree of glyphOnly) expect(thumbnails(tree)).toEqual([]);
    expect(types(glyphOnly[1])).toContain(Video);
    // A row without the thumbnail's fields, the sample workspace's, is an image.
    expect(types(glyphOnly[2])).toContain(ImageIcon);
  });
});
