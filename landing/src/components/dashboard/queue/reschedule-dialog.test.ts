import { describe, expect, it } from "vitest";
import { NO_REPEAT } from "@/lib/repeat";
import { rescheduleChange } from "./reschedule-dialog";

/**
 * What one press of Reschedule… or Edit series… sends (#485): only what
 * differs from what the dialog opened on. The dialog holds state and the test
 * environment has no DOM, so its decision is pinned here.
 */

const at = "2026-10-15T16:07";
const later = "2026-10-16T09:00";
const weekly = { unit: "week" as const, every: 1, days: [1, 4] };
const oneOff = { localAt: at, form: NO_REPEAT };
const series = { localAt: at, form: weekly };

describe("rescheduleChange", () => {
  it("sends a moved time, and nothing when nothing changed", () => {
    expect(rescheduleChange(oneOff, { ...oneOff, localAt: later })).toEqual({ localAt: later });
    expect(rescheduleChange(oneOff, oneOff)).toBeNull();
    expect(rescheduleChange(series, series)).toBeNull();
  });

  it("sends a series' rule only when it changed, in whatever order its days were pressed", () => {
    expect(rescheduleChange(series, { ...series, localAt: later })).toEqual({ localAt: later });
    expect(rescheduleChange(series, { localAt: at, form: { ...weekly, days: [4, 1] } })).toBeNull();
    expect(rescheduleChange(series, { localAt: at, form: { ...weekly, every: 2 } })).toEqual({
      repeat: { unit: "week", every: 2, days: [1, 4] },
    });
  });

  it("clears the rule when Edit series is set to Never", () => {
    expect(rescheduleChange(series, { localAt: at, form: NO_REPEAT })).toEqual({ repeat: null });
  });
});
