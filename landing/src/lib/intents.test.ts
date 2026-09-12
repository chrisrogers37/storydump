import { describe, it, expect } from "vitest";
import {
  ACTION_LABELS,
  INTENT_STATES,
  NON_TERMINAL_STATES,
  QUEUE_ACTIONS,
  QUEUE_COMMANDS,
  accountLabel,
  actionsFor,
  formatSlot,
  isQueueCommand,
  refusalCopy,
  requestFor,
} from "./intents";
import { idempotencyKeyFor } from "./commands";

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

  it("offers nothing on a card whose cancellation is requested — its destination may be gone", () => {
    expect(actionsFor("awaiting_approval", true, true)).toEqual([]);
    expect(actionsFor("awaiting_approval", false, true)).toEqual([]);
  });

  it("offers the three resolutions on review_required — the workspace resolves its own review (2026-09-12)", () => {
    expect(actionsFor("review_required", true, false, "publish_called")).toEqual([
      "resolve_posted",
      "retry",
      "resolve_cancel",
    ]);
    // Post again re-mints the publish job, so it needs the API to publish.
    expect(actionsFor("review_required", false, false, "publish_called")).toEqual([
      "resolve_posted",
      "resolve_cancel",
    ]);
    // It posted needs a publish call to confirm: before that rung the port
    // can only refuse it, so the button is not offered (the row's step says).
    expect(actionsFor("review_required", true, false, "container_ready")).toEqual([
      "retry",
      "resolve_cancel",
    ]);
    expect(actionsFor("review_required", true)).toEqual(["retry", "resolve_cancel"]);
    // A cancel already asked for is honoured by Give up alone.
    expect(actionsFor("review_required", true, true, "publish_called")).toEqual([
      "resolve_cancel",
    ]);
  });

  it("offers nothing on every other state — those rows are the ledger's read-only view", () => {
    for (const state of INTENT_STATES) {
      if (state === "awaiting_approval" || state === "review_required") continue;
      expect(actionsFor(state, true), state).toEqual([]);
      expect(actionsFor(state, false), state).toEqual([]);
    }
  });

  it("maps every action to one command and the body the port reads", () => {
    const intent = { id: "0b6e5f1a-2f4d-4c1e-9a3b-7d8e9f0a1b2c", entered_state_at: "2026-09-12T10:00:00+00:00" };
    expect(requestFor("approve", intent)).toEqual({
      command: "approve",
      body: { intent_id: intent.id },
    });
    // Post again carries the member's verdict: the web asks "is it on your
    // story?" first, and the port needs the answer when the publish answer
    // was lost (a plain retry could post the story twice).
    expect(requestFor("retry", intent)).toEqual({
      command: "resolve_review",
      body: {
        intent_id: intent.id,
        resolution: "retry",
        verdict: "not_posted",
        episode: intent.entered_state_at,
      },
    });
    expect(requestFor("resolve_posted", intent).body.resolution).toBe("posted");
    expect(requestFor("resolve_cancel", intent).body.resolution).toBe("cancel");
    for (const action of QUEUE_ACTIONS) {
      expect(ACTION_LABELS[action], action).toBeTruthy();
      expect(isQueueCommand(requestFor(action, intent).command), action).toBe(true);
    }
  });

  it("knows the non-terminal states the page lists", () => {
    expect(NON_TERMINAL_STATES).toEqual([
      "scheduled",
      "prompt_pending",
      "awaiting_approval",
      "approved",
      "publishing",
      "publishing_ambiguous",
      "review_required",
    ]);
  });
});

describe("the command allowlist", () => {
  it("admits exactly the five commands the queue fronts and nothing else the vocabulary knows", () => {
    expect(QUEUE_COMMANDS).toEqual([
      "approve",
      "mark_posted",
      "skip",
      "reject",
      "resolve_review",
    ]);
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
  it("names the review card's two refusals", () => {
    expect(refusalCopy("nothing_to_confirm")).toMatch(/nothing to confirm/i);
    expect(refusalCopy("may_have_posted")).toMatch(/It posted/);
  });

  it("turns the matrix's normal 409 answers into a sentence, never a raw code", () => {
    expect(refusalCopy("illegal_transition")).toMatch(/already/i);
    expect(refusalCopy("manual_mode")).toMatch(/Posted myself/);
    expect(refusalCopy("not_found")).toMatch(/no longer/i);
  });

  it("separates the person's session from the router being down", () => {
    // #1140: this asserted /sign in/i, which passed on "Sign in again." — the
    // unhedged imperative that sent the first real user round a loop it could
    // not resolve. The DISTINCTION this test is named for is the property
    // worth pinning, so it is asserted directly: the two branches must not
    // render the same sentence. The session branch's own wording is pinned
    // once, against a shared source, in `refusal-copy.test.ts`.
    expect(refusalCopy("unauthenticated")).not.toBe(
      refusalCopy("target_router_unreachable"),
    );
    expect(refusalCopy("unauthenticated")).toMatch(/not signed in/i);
    expect(refusalCopy("target_router_unreachable")).toMatch(/Storydump/);
  });

  it("has a fallback for a reason it does not know — or no reason at all — and the fallback names nobody at fault", () => {
    for (const reason of ["something_new", undefined, 42]) {
      const copy = refusalCopy(reason);
      expect(copy.length).toBeGreaterThan(0);
      expect(copy).not.toContain("something_new");
      expect(copy).not.toMatch(/you/i);
    }
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

