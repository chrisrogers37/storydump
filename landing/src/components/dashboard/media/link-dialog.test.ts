import { describe, expect, it } from "vitest";
import { canSaveLink } from "./link-dialog";

describe("canSaveLink: what Save link may send", () => {
  it("saves a typed link over none, or over a different one", () => {
    expect(canSaveLink("https://example.com/menu", null)).toBe(true);
    expect(canSaveLink("https://example.com/new", "https://example.com/menu")).toBe(true);
  });

  it("saves nothing from a blank field: clearing is Remove link's act", () => {
    expect(canSaveLink("", null)).toBe(false);
    expect(canSaveLink("   ", "https://example.com/menu")).toBe(false);
  });

  it("saves nothing when the link is the one already there", () => {
    expect(canSaveLink("https://example.com/menu", "https://example.com/menu")).toBe(false);
    expect(canSaveLink("  https://example.com/menu ", "https://example.com/menu")).toBe(false);
  });
});
