import { describe, expect, it } from "vitest";
import { thumbnailSrc } from "./thumbnails";

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
