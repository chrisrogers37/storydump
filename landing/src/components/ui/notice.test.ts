import { describe, expect, it } from "vitest";
import { NOTICE_CLASS, noticeRole, type NoticeTone } from "./notice";

/**
 * The defect this component exists for was an ACCESSIBILITY one, and it was
 * invisible: four of nine error boxes announced themselves and five did
 * not, and nothing on screen said which. The role is now a function of the
 * tone, so this is the whole of that rule in two cases.
 */
describe("a notice announces itself according to its tone", () => {
  it("is assertive for an error and polite for the rest", () => {
    expect(noticeRole("error")).toBe("alert");
    expect(noticeRole("success")).toBe("status");
    expect(noticeRole("info")).toBe("status");
  });

  it("has one class string per tone, and only error is red", () => {
    for (const tone of [
      "error",
      "success",
      "info",
    ] as const satisfies readonly NoticeTone[]) {
      expect(NOTICE_CLASS[tone], tone).toBeTruthy();
    }
    expect(NOTICE_CLASS.error).toContain("red");
    expect(NOTICE_CLASS.success).not.toContain("red");
    expect(NOTICE_CLASS.info).not.toContain("red");
  });

  it("gives the two green tones ONE shade, which is the whole of the D9 colour fix", () => {
    // Seven green banners, two of which read `text-green-900`. A second shade
    // is only ever noticed as "these two look slightly off", which nobody
    // files, so it is pinned rather than left to a reviewer's eye.
    expect(NOTICE_CLASS.success).toContain("text-green-800");
    expect(NOTICE_CLASS.success).not.toContain("text-green-900");
  });
});
