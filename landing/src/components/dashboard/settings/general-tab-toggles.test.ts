import { describe, expect, it } from "vitest";

import { isLiveToggle, TOGGLES } from "./general-tab";

/**
 * #1155 — a switch may be live only if the port accepts the write AND
 * something reads the value.
 *
 * The defect this pins is invisible by construction: `dry_run_mode` and
 * `enable_ai_captions` drew a working switch, saved successfully, returned no
 * error, and nothing in `src/services/target/` ever read them. A dead control
 * disappoints; a save that confirms and does nothing **manufactures a belief**
 * — the person walks away certain of something false, with no error and no
 * reason to check. There is no complaint to route on and nothing to find later.
 *
 * **Which makes the test shape matter more than usual.** A test asserting the
 * three are inert would pass the moment someone flips them back and adds a
 * reason string. So the assertion is inverted: the LIVE set is pinned to an
 * explicit list. Making any toggle live is then a deliberate edit here, in a
 * file whose name says what the edit costs — the same ratchet
 * `commands.UNBUILT` uses, and for the same reason.
 *
 * **What this cannot check:** whether a consumer actually exists in the target
 * tier. That is Python, on the other side of an HTTP boundary, and no test in
 * this suite can see it. This pins the DECISION, not the fact. The facts, as
 * measured at `d995af5`: `dry_run_mode` 0 target-tier readers,
 * `enable_ai_captions` 0, `api_publishing_enabled` consumed but its
 * `publish_pipeline` job parks on `media_fetch=None`.
 */

/**
 * Toggles that draw a working switch. **Empty, deliberately** — every toggle
 * on this tab is currently inert, and that is the finding rather than a
 * mistake in this list.
 */
const LIVE_TOGGLES: readonly string[] = [];

// `isLiveToggle` is IMPORTED, not restated. An earlier version of this file
// copied the predicate, and a mutant that reverted the component's rule to
// `settingsKey !== null` passed all four tests — the copy agreed with itself
// while the shipped rule changed underneath it. That is the same shape as the
// defect under test: a check that cannot fail for the thing it is named after.
const isLive = isLiveToggle;

describe("a live switch requires a consumer, not just a column", () => {
  it("the live set is exactly the declared one", () => {
    expect(TOGGLES.filter(isLive).map((t) => t.key)).toEqual([...LIVE_TOGGLES]);
  });

  it("every toggle that is not live says why, in its own words", () => {
    for (const t of TOGGLES.filter((x) => !isLive(x))) {
      expect(t.inertReason ?? "", t.key).not.toBe("");
    }
  });

  it("the three #1155 toggles keep their real settingsKey", () => {
    // Saying `null` would claim the port has no such setting — false, and it
    // would send the next person to add a command that already exists. The
    // reason they are inert is the missing CONSUMER, not a missing command.
    for (const key of ["dry_run_mode", "enable_instagram_api", "enable_ai_captions"]) {
      const row = TOGGLES.find((t) => t.key === key);
      expect(row, key).toBeDefined();
      expect(row!.settingsKey, key).not.toBeNull();
      expect(row!.inertReason, key).toBeTruthy();
    }
  });

  it("no inert reason names a cause the code did not establish", () => {
    // #1140's rule, applied here: these sentences say what is not built. None
    // may claim WHY in a way nothing checked, and none may be vague enough to
    // hide the defect rather than state it — softening the confirmation was
    // the wrong repair this fix exists to avoid.
    for (const t of TOGGLES) {
      if (!t.inertReason) continue;
      expect(t.inertReason.length, t.key).toBeGreaterThan(12);
      expect(t.inertReason.toLowerCase(), t.key).not.toContain("error");
      expect(t.inertReason.toLowerCase(), t.key).not.toContain("something went wrong");
    }
  });
});
