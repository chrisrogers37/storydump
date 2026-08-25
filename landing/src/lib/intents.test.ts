import { describe, it, expect } from "vitest";
import {
  INTENT_STATES,
  NON_TERMINAL_STATES,
  QUEUE_COMMANDS,
  accountLabel,
  actionsFor,
  formatSlot,
  idempotencyKeyFor,
  isIntentId,
  isQueueCommand,
  refusalCopy,
} from "./intents";

/**
 * The queue's pure decisions, pinned away from the components that render
 * them. Which buttons an intent gets is the `02` §4 matrix (every user edge
 * leaves `awaiting_approval`; nothing else has a human lever), and a wrong
 * answer here is not a styling bug — it is a button that posts a 409, or a
 * missing button on the one state a person can act on.
 */

describe("which actions an intent offers", () => {
  it("offers the manual taps on awaiting_approval, and Approve only when the API can publish", () => {
    expect(actionsFor("awaiting_approval", false)).toEqual([
      "mark_posted",
      "skip",
      "reject",
    ]);
    // Hybrid keeps the manual buttons beside Approve (`06` §3).
    expect(actionsFor("awaiting_approval", true)).toEqual([
      "approve",
      "mark_posted",
      "skip",
      "reject",
    ]);
  });

  it("offers nothing on every other state — those rows are the ledger's read-only view", () => {
    for (const state of INTENT_STATES) {
      if (state === "awaiting_approval") continue;
      expect(actionsFor(state, true), state).toEqual([]);
      expect(actionsFor(state, false), state).toEqual([]);
    }
  });

  it("knows the non-terminal states the page lists, and that they exclude the terminal ones", () => {
    expect(NON_TERMINAL_STATES).toEqual([
      "scheduled",
      "prompt_pending",
      "awaiting_approval",
      "approved",
      "publishing",
      "publishing_ambiguous",
      "review_required",
    ]);
    for (const terminal of ["posted", "skipped", "rejected", "expired", "failed", "cancelled"]) {
      expect(NON_TERMINAL_STATES).not.toContain(terminal);
    }
  });
});

describe("the command allowlist", () => {
  it("admits exactly the four v1 commands and nothing else the vocabulary knows", () => {
    expect(QUEUE_COMMANDS).toEqual(["approve", "mark_posted", "skip", "reject"]);
    for (const c of QUEUE_COMMANDS) expect(isQueueCommand(c), c).toBe(true);
    // Real vocabulary names that the queue must NOT forward: `cancel` has no
    // audit row and `autopost_now` is unbuilt (501) — a follow-up each.
    for (const c of ["cancel", "autopost_now", "settings_change", "", " approve", "APPROVE", undefined, 42]) {
      expect(isQueueCommand(c), String(c)).toBe(false);
    }
  });
});

describe("the idempotency key", () => {
  it("is stable per (command, intent) so a double-click replays rather than re-executes", () => {
    const id = "0b6e5f1a-2f4d-4c1e-9a3b-7d8e9f0a1b2c";
    expect(idempotencyKeyFor("mark_posted", id)).toBe(`mark_posted:${id}`);
    expect(idempotencyKeyFor("mark_posted", id)).toBe(idempotencyKeyFor("mark_posted", id));
    // A different command on the same intent is a different key: a refused
    // Approve must not shadow a later Skip.
    expect(idempotencyKeyFor("skip", id)).not.toBe(idempotencyKeyFor("mark_posted", id));
    // The API caps the header at 200 characters.
    expect(idempotencyKeyFor("mark_posted", id).length).toBeLessThanOrEqual(200);
  });
});

describe("refusal copy", () => {
  it("turns the matrix's normal 409 answers into a sentence, never a raw code", () => {
    expect(refusalCopy("illegal_transition", 409)).toMatch(/already/i);
    expect(refusalCopy("manual_mode", 409)).toMatch(/Posted myself/);
    expect(refusalCopy("not_found", 404)).toMatch(/no longer/i);
  });

  it("separates the person's session from the router being down", () => {
    expect(refusalCopy("unauthenticated", 401)).toMatch(/sign in/i);
    expect(refusalCopy("target_router_unreachable", 503)).toMatch(/Storydump/);
  });

  it("has a fallback for a reason it does not know, and the fallback names nobody at fault", () => {
    const copy = refusalCopy("something_new", 500);
    expect(copy.length).toBeGreaterThan(0);
    expect(copy).not.toContain("something_new");
    expect(copy).not.toMatch(/you/i);
  });
});

describe("the account label", () => {
  it("prefers the handle, falls back to the display name, and never renders an empty cell", () => {
    expect(accountLabel({ account_handle: "northside", account_display_name: "Northside Coffee" })).toBe("northside");
    expect(accountLabel({ account_handle: null, account_display_name: "Northside Coffee" })).toBe("Northside Coffee");
    expect(accountLabel({ account_handle: null, account_display_name: null })).toBe("Account");
    expect(accountLabel({ account_handle: "", account_display_name: "" })).toBe("Account");
  });
});

describe("the slot, in the workspace's clock", () => {
  it("renders the same instant differently across time zones — a solo user reads their own clock, never UTC", () => {
    const iso = "2026-08-25T18:30:00+00:00";
    const ny = formatSlot(iso, "America/New_York");
    const tokyo = formatSlot(iso, "Asia/Tokyo");
    expect(ny).toContain("2:30");
    expect(tokyo).toContain("3:30");
    expect(ny).not.toBe(tokyo);
  });

  it("survives a fractional-seconds timestamp of any width, which is how Postgres renders them", () => {
    expect(formatSlot("2026-08-25T18:30:00.1+00:00", "UTC")).toContain("6:30");
    expect(formatSlot("2026-08-25T18:30:00.123456+00:00", "UTC")).toContain("6:30");
  });

  it("does not throw on a time zone Intl does not know — a render must never crash the whole list", () => {
    expect(() => formatSlot("2026-08-25T18:30:00+00:00", "Mars/Olympus_Mons")).not.toThrow();
    expect(formatSlot("2026-08-25T18:30:00+00:00", "Mars/Olympus_Mons")).toContain("6:30");
  });
});

describe("the intent id the browser sends", () => {
  it("is a UUID or nothing — the route handler never forwards a free-form string into a command", () => {
    expect(isIntentId("0b6e5f1a-2f4d-4c1e-9a3b-7d8e9f0a1b2c")).toBe(true);
    expect(isIntentId("0B6E5F1A-2F4D-4C1E-9A3B-7D8E9F0A1B2C")).toBe(true);
    for (const bad of ["", "not-a-uuid", "0b6e5f1a-2f4d-4c1e-9a3b-7d8e9f0a1b2c ", 42, null, undefined, {}]) {
      expect(isIntentId(bad), String(bad)).toBe(false);
    }
  });
});
