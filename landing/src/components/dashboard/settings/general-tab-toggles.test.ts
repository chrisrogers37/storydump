import { describe, expect, it } from "vitest";

import { isLiveToggle, scheduleSavedNotice, TOGGLES } from "./general-tab";

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
// All three remaining switches are live (owner, 2026-09-10): pausing through
// its two commands, dry run and the Instagram API through `settings_change`,
// each with a consumer on the other side (the clock/prompt sweep/publish leg,
// the pipeline's dry-run branch, the publish leg).
const LIVE_TOGGLES: readonly string[] = [
  "is_paused",
  "dry_run_mode",
  "enable_instagram_api",
];

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

  it("pausing is two commands, not a settings key", () => {
    const row = TOGGLES.find((t) => t.key === "is_paused")!;
    expect(row.settingsKey).toBeNull();
    expect(row.command).toEqual({
      on: "pause_workspace",
      off: "resume_workspace",
    });
  });

  it("the settings-backed switches keep their real settingsKey", () => {
    for (const [key, settingsKey] of [
      ["dry_run_mode", "dry_run_mode"],
      ["enable_instagram_api", "api_publishing_enabled"],
    ]) {
      const row = TOGGLES.find((t) => t.key === key);
      expect(row?.settingsKey, key).toBe(settingsKey);
    }
  });

  it("the four rows with no source on this tier are gone", () => {
    const keys = TOGGLES.map((t) => t.key as string);
    for (const gone of [
      "enable_ai_captions",
      "show_verbose_notifications",
      "send_lifecycle_notifications",
      "media_sync_enabled",
    ]) {
      expect(keys, gone).not.toContain(gone);
    }
  });

  it("no inert reason names a cause the code did not establish", () => {
    for (const t of TOGGLES) {
      if (!t.inertReason) continue;
      expect(t.inertReason.length, t.key).toBeGreaterThan(12);
      expect(t.inertReason.toLowerCase(), t.key).not.toContain("error");
      expect(t.inertReason.toLowerCase(), t.key).not.toContain(
        "something went wrong",
      );
    }
  });
});

describe("scheduleSavedNotice", () => {
  it("says a post already on the clock keeps its time, for accounts on the workspace schedule", () => {
    const text = scheduleSavedNotice("active");
    expect(text).toMatch(/^Schedule saved\./);
    expect(text).toContain("keeps its time");
    expect(text).toContain("accounts on the workspace schedule");
  });

  it("says a deleted workspace's schedule applies once it is restored", () => {
    expect(scheduleSavedNotice("offboarding")).toBe(
      "Schedule saved. It applies once the workspace is restored.",
    );
  });
});
