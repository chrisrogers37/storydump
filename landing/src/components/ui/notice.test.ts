import { describe, expect, it } from "vitest";
import { NOTICE_CLASS, noticeRole, type NoticeTone } from "./notice";

/**
 * The defect this component exists for is an ACCESSIBILITY one, and it is
 * invisible: an error box that does not announce itself looks the same as one
 * that does. The role is a function of the tone, so this is the whole of that
 * rule in two cases.
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

  it("gives a success notice ONE green shade", () => {
    // A second shade is only ever noticed as "these two look slightly off",
    // which nobody files, so it is pinned rather than left to a reviewer's eye.
    expect(NOTICE_CLASS.success).toContain("text-green-800");
    expect(NOTICE_CLASS.success).not.toContain("text-green-900");
  });
});
