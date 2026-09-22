/**
 * A nullable workspace setting, shown honestly.
 *
 * `repost_ttl_days` and `skip_ttl_days` are declared NULL on purpose, so that
 * "the deployment's fallback applies" stays distinguishable from "the owner
 * chose this number" (`src/config/defaults.py`). The cards must therefore show
 * something for an unset field, and they used to do it with `?? 30` / `?? 45`
 * — a second copy of a backend number. It drifted: the card said 30 while a
 * worker publish locked the media for 7 (#1365).
 *
 * The fallback now rides on the config payload (`workspaces.get_workspace`
 * serves `defaults`), the way `restorable_until` already does "so the
 * dashboard never derives it from a copied number" (#1127). This is the small
 * amount of logic that goes with it.
 */
export type SettingField = {
  /** What the input shows: the stored value, or the deployment's fallback. */
  value: number;
  /** True when nothing is stored, so the field can be rendered as a default. */
  usingDefault: boolean;
  /** Whether an entered value differs from WHAT IS STORED. */
  isChanged: (entered: number) => boolean;
};

export function settingField(
  stored: number | null,
  fallback: number,
): SettingField {
  return {
    value: stored ?? fallback,
    usingDefault: stored === null,
    // Against `stored`, never against the fallback. Comparing to the fallback
    // is the change-detection half of #1366: with nothing stored, typing the
    // default read as "no change", Save stayed disabled, and the one value a
    // person most plausibly wants to pin was the one they could not save.
    // It matters even when the fallback is right — an explicit 30 survives a
    // change to the deployment's default, a null follows it.
    isChanged: (entered: number) => entered !== stored,
  };
}
