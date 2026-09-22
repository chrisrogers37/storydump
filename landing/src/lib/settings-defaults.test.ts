import { describe, expect, it } from "vitest";
import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import path from "path";
import { settingField } from "./settings-defaults";

/**
 * A nullable setting shows the deployment's fallback, and saving it works.
 *
 * Two defects, one shape (#1366). The cards wrote `?? 30` and `?? 45`:
 *
 *   - a second copy of a backend number, which drifted the moment the
 *     worker's own copy did — the card said 30 while a worker publish locked
 *     for 7 (#1365); and
 *   - the same fabricated value was the baseline for change-detection, so
 *     `repost !== (repostTtlDays ?? 30)` is false when nothing is stored and
 *     the person types 30. Save never enables, and the value is never pinned.
 *
 * The fallback now arrives on the config payload (`workspaces.get_workspace`),
 * and `isChanged` compares against the STORED value, so both go together:
 * null -> 30 is a change, because it is.
 */
const CARD = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../components/dashboard/settings/repost-cadence-card.tsx",
);
const GENERAL_TAB = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../components/dashboard/settings/general-tab.tsx",
);

describe("settingField — what an unset setting shows and whether it saves", () => {
  it("shows the deployment's fallback when nothing is stored, and says so", () => {
    const f = settingField(null, 30);
    expect(f.value).toBe(30);
    expect(f.usingDefault).toBe(true);
  });

  it("shows the stored value once set, and stops calling it a default", () => {
    const f = settingField(7, 30);
    expect(f.value).toBe(7);
    expect(f.usingDefault).toBe(false);
  });

  it("TYPING THE DEFAULT INTO AN UNSET FIELD IS A CHANGE", () => {
    // The whole change-detection defect. `?? 30` made this false, so the one
    // value a person most plausibly wants to pin was the one they could not
    // save. It matters even when the fallback is correct: an explicit 30
    // survives a change to the deployment's default; a null does not.
    expect(settingField(null, 30).isChanged(30)).toBe(true);
  });

  it("is not a change when it matches what is stored", () => {
    expect(settingField(30, 30).isChanged(30)).toBe(false);
    expect(settingField(7, 30).isChanged(7)).toBe(false);
  });

  it("is a change when it differs from what is stored", () => {
    expect(settingField(null, 30).isChanged(14)).toBe(true);
    expect(settingField(7, 30).isChanged(14)).toBe(true);
  });
});

describe("the cards hold no copy of a backend number", () => {
  it("the repost card has no literal fallback in it", () => {
    // The guard that makes serving the defaults worth doing. Without it this
    // change is the same bug with better manners: a frontend copy is free to
    // drift however politely it is displayed.
    const source = readFileSync(CARD, "utf8");
    const code = source.replace(/\/\*[\s\S]*?\*\/|\/\/.*$/gm, "");
    expect(code).not.toMatch(/\?\?\s*\d+/);
    expect(code).not.toMatch(/\b(30|45)\b/);
  });

  it("the settings tab does not render a control that changes nothing", () => {
    // `caption_style` is read by no code at all: `prompts.render_card` takes
    // no style argument and always emits the emoji form, which is exactly the
    // "Enhanced" option's own description. "Simple" saved successfully and
    // changed nothing. Removed the way CategoryMixCard was removed in this
    // same file — not re-gated behind a flag — and it comes back when
    // `render_card` can honour it.
    const source = readFileSync(GENERAL_TAB, "utf8");
    const code = source.replace(/\/\*[\s\S]*?\*\/|\/\/.*$/gm, "");
    expect(code).not.toMatch(/<CaptionStyleCard/);
  });
});
