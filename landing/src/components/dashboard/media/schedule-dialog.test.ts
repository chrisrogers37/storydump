import { describe, expect, it } from "vitest";
import {
  accountChoiceLabel,
  scheduledOutcome,
  schedulePlan,
  scheduleStep,
  scheduleZone,
  zoneNote,
} from "./schedule-dialog";

/**
 * The Schedule dialog's pure decisions (#1413 phase 6).
 *
 * The dialog holds state and the test environment has no DOM, so it cannot be
 * called here; what it SAYS is decided by these functions and pinned here.
 */

describe("scheduleZone — the clock a planned time is read on", () => {
  it("is the account's own zone when it has one", () => {
    expect(scheduleZone({ tz: "Europe/Paris" }, "America/New_York")).toEqual({
      zone: "Europe/Paris",
      source: "account",
    });
  });

  it("is the workspace's when the account has none, as the port reads it", () => {
    expect(scheduleZone({ tz: null }, "America/New_York")).toEqual({
      zone: "America/New_York",
      source: "workspace",
    });
  });

  it("is UTC when neither has one, the port's own fallback", () => {
    expect(scheduleZone({ tz: null }, null)).toEqual({ zone: "UTC", source: "default" });
  });
});

describe("zoneNote", () => {
  it("names the zone and whose it is", () => {
    expect(zoneNote({ zone: "Europe/Paris", source: "account" })).toMatch(
      /Europe\/Paris.*account/,
    );
    expect(zoneNote({ zone: "America/New_York", source: "workspace" })).toMatch(
      /America\/New_York.*workspace/,
    );
  });

  it("says why it is UTC, rather than implying someone chose it", () => {
    expect(zoneNote({ zone: "UTC", source: "default" })).toMatch(/UTC.*no time zone/i);
  });
});

describe("accountChoiceLabel", () => {
  it("names an active account the way its Accounts row is titled", () => {
    expect(
      accountChoiceLabel({ display_name: "Story Co", handle: "storyco", state: "active" }),
    ).toBe("Story Co");
  });

  it("carries the state of an account that is not active", () => {
    expect(
      accountChoiceLabel({ display_name: null, handle: "storyco", state: "reauth_required" }),
    ).toBe("storyco — Reconnect needed");
  });
});

describe("scheduledOutcome — what the dialog may say once the port has planned it", () => {
  const planned = {
    outcome: "executed",
    schedule_slot_at: "2026-10-03T18:30:00+00:00",
    tz: "Asia/Tokyo",
    warnings: [],
  };

  it("says when it is asked, on the clock the port read it on", () => {
    // 18:30 UTC is 03:30 the next morning in Tokyo. The zone differs from both
    // UTC and New York, so a regression to the process's own clock fails on
    // any machine rather than only on some.
    expect(scheduledOutcome(planned).when).toMatch(/Oct 4.*3:30/);
  });

  it("flags a workspace with no chat bound, where nothing is asked until one is", () => {
    expect(scheduledOutcome({ ...planned, warnings: ["no_push_binding"] }).noChat).toBe(true);
    expect(scheduledOutcome(planned).noChat).toBe(false);
  });

  it("claims no time it was not given", () => {
    expect(scheduledOutcome({ outcome: "executed" }).when).toBeNull();
  });
});

describe("scheduleStep — where one answer from the port leaves the dialog", () => {
  const overridable = {
    ok: false as const,
    error: "locked",
    status: 409,
    facts: { overridable: true, in_the_way: ["recent"] },
  };

  it("is done when the port planned it", () => {
    expect(
      scheduleStep({ ok: true, data: { outcome: "executed", warnings: [] } }, false),
    ).toMatchObject({ kind: "done" });
  });

  it("asks before overriding a lock the port says can be overridden", () => {
    expect(scheduleStep(overridable, false)).toEqual({
      kind: "confirm",
      copy: "It was posted on this account recently. Schedule it anyway?",
    });
  });

  it("never offers the override for a blocker", () => {
    expect(
      scheduleStep(
        { ok: false, error: "locked", status: 409, facts: { overridable: false, in_the_way: ["reject"] } },
        false,
      ).kind,
    ).toBe("refused");
  });

  it("never offers it without the port's word that it can be overridden", () => {
    expect(scheduleStep({ ok: false, error: "locked", status: 409 }, false).kind).toBe("refused");
  });

  it("takes the port's word over the lock kinds: a warning beside a blocker is refused", () => {
    // `skip` alone could be overridden; a removed item cannot be, and the port
    // says so. A guess from the kinds would offer the override here.
    expect(
      scheduleStep(
        {
          ok: false,
          error: "locked",
          status: 409,
          facts: { overridable: false, in_the_way: ["item_removed", "skip"] },
        },
        false,
      ).kind,
    ).toBe("refused");
  });

  it("does not ask twice: a refusal of the override itself is final", () => {
    expect(scheduleStep(overridable, true).kind).toBe("refused");
  });
});

describe("schedulePlan — what one press sends", () => {
  const pick = {
    accountId: "55555555-5555-4555-8555-555555555555",
    itemId: "66666666-6666-4666-8666-666666666666",
    localAt: "2026-10-03T14:30",
  };

  it("sends the override from the question the port's word opened", () => {
    expect(schedulePlan("confirm", pick)).toStrictEqual({
      accountId: "55555555-5555-4555-8555-555555555555",
      itemId: "66666666-6666-4666-8666-666666666666",
      localAt: "2026-10-03T14:30",
      overrideLocks: true,
    });
  });

  it("never sends it from the form", () => {
    expect(schedulePlan("form", pick)).toStrictEqual({
      accountId: "55555555-5555-4555-8555-555555555555",
      itemId: "66666666-6666-4666-8666-666666666666",
      localAt: "2026-10-03T14:30",
    });
  });
});
