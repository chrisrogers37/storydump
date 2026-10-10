import { describe, expect, it } from "vitest";
import { thumbnailFor, thumbnailSrc } from "./thumbnails";

describe("thumbnailSrc (#1634)", () => {
  it("names this tier's route, never a provider's host, and carries the version", () => {
    expect(thumbnailSrc("ws-1", "media-1", "0123456789abcdef")).toBe(
      "/api/workspaces/ws-1/media/media-1/thumbnail?v=0123456789abcdef",
    );
  });

  it("encodes every part, so none can reshape the path", () => {
    expect(thumbnailSrc("a/b", "c?d", "e&f")).toBe(
      "/api/workspaces/a%2Fb/media/c%3Fd/thumbnail?v=e%26f",
    );
  });
});

describe("thumbnailFor (#1634)", () => {
  const row = {
    file_name: "a.jpg",
    media_kind: "image",
    media_item_id: "media-1",
    has_thumbnail: true,
    thumbnail_version: "v1",
  };

  it("is the route for a row that has a picture, in a workspace", () => {
    expect(thumbnailFor(row, "ws-1")).toBe("/api/workspaces/ws-1/media/media-1/thumbnail?v=v1");
  });

  it("is nothing without a picture, or without a workspace to ask", () => {
    expect(thumbnailFor({ ...row, has_thumbnail: false }, "ws-1")).toBeNull();
    expect(thumbnailFor(row, null)).toBeNull();
    expect(thumbnailFor(row, undefined)).toBeNull();
  });

  it("is nothing for a row without the thumbnail's fields, as the sample's calendar draws", () => {
    expect(thumbnailFor({ file_name: "a.jpg" }, "ws-1")).toBeNull();
  });
});
