import { describe, expect, it } from "vitest";
import { TONE_CLASS, type BadgeTone } from "./tone";
import { destinationStateBadge } from "@/lib/destination";
import { driveStatusBadge } from "@/lib/drive";
import { tokenStateBadge } from "@/components/dashboard/settings/api-tokens-tab";

/**
 * GREEN IS A CLAIM, and three files used to make it in prose only.
 *
 * Each of `destination.ts`, `drive.ts` and `api-tokens-tab.tsx` carried a
 * comment saying "only `active` is ever green", and each owned its own copy
 * of the class map, so the sentence was true by coincidence three times.
 * The map is one object now; this is the assertion that the sentence was
 * describing.
 */
describe("the badge tone vocabulary", () => {
  it("gives green to `active` and to nothing else", () => {
    const green = (tone: BadgeTone) => TONE_CLASS[tone].includes("green");
    expect(green("active")).toBe(true);
    expect(green("attention")).toBe(false);
    expect(green("inert")).toBe(false);
  });

  it("has a class for every tone", () => {
    for (const tone of ["active", "attention", "inert"] as const) {
      expect(TONE_CLASS[tone], tone).toBeTruthy();
    }
  });

  it("is the union the three badge deciders answer in", () => {
    // The union has ONE declaration now, so this is a compile-time fact as
    // much as a runtime one — the three `Record`s below would not type-check
    // against a different set of tones. Asserted anyway because the point of
    // the extraction is that these three agree, and a test says so to someone
    // reading the report rather than the types.
    const tones: BadgeTone[] = [
      destinationStateBadge("active").tone,
      destinationStateBadge("disabled").tone,
      driveStatusBadge("active").tone,
      driveStatusBadge("revoked").tone,
      tokenStateBadge("live").tone,
      tokenStateBadge("revoked").tone,
    ];
    for (const tone of tones) {
      expect(TONE_CLASS[tone], tone).toBeTruthy();
    }
    expect(destinationStateBadge("active").tone).toBe("active");
    expect(driveStatusBadge("active").tone).toBe("active");
    expect(tokenStateBadge("live").tone).toBe("active");
  });
});
