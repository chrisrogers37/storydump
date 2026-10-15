import { describe, it, expect } from "vitest";
import {
  ACTION_LABELS,
  INTENT_STATES,
  NON_TERMINAL_STATES,
  QUEUE_ACTIONS,
  QUEUE_COMMANDS,
  accountLabel,
  actionsFor,
  approvedBy,
  formatSlot,
  isQueueCommand,
  refusalCopy,
  requestFor,
  plannedStoryHref,
  storyAnchor,
} from "./intents";
import { idempotencyKeyFor } from "./commands";
import type { RepeatRule } from "./repeat";

const LINK = "https://example.com/drop";
const WEEKLY: RepeatRule = { unit: "week", every: 1, days: [1, 4], at: "16:07" };

/**
 * The queue's pure decisions, pinned away from the components that render
 * them. Which buttons an intent gets is the matrix (every user edge
 * leaves `awaiting_approval`; nothing else has a human lever), and a wrong
 * answer here is not a styling bug — it is a button that posts a 409, or a
 * missing button on the one state a person can act on.
 */

describe("which actions an intent offers", () => {
  it("offers the manual taps on awaiting_approval, and Approve only when the API can publish", () => {
    expect(actionsFor({ state: "awaiting_approval" }, false)).toEqual([
      "mark_posted",
      "skip",
      "reject",
    ]);
    // Hybrid keeps the manual buttons beside Approve.
    expect(actionsFor({ state: "awaiting_approval" }, true)).toEqual([
      "approve",
      "mark_posted",
      "skip",
      "reject",
    ]);
  });

  it("offers no Approve for a story whose item has a link to add by hand", () => {
    // An app cannot attach a link to a story it publishes, so the story is posted by hand.
    const linked = actionsFor({ state: "awaiting_approval", origin: "cadence", link_url: LINK }, true);
    expect(linked).toEqual(["mark_posted", "skip", "reject"]);
    expect(
      actionsFor({ state: "awaiting_approval", origin: "planned", link_url: LINK }, true),
    ).toEqual(linked);
    // Without a link, the same story keeps Approve.
    expect(
      actionsFor({ state: "awaiting_approval", origin: "cadence", link_url: null }, true),
    ).toContain("approve");
  });

  it("offers nothing on a card whose cancellation is requested — its destination may be gone", () => {
    expect(actionsFor({ state: "awaiting_approval", cancel_requested: true }, true)).toEqual([]);
    expect(actionsFor({ state: "awaiting_approval", cancel_requested: true }, false)).toEqual([]);
  });

  it("offers the three resolutions on review_required — the workspace resolves its own review", () => {
    expect(actionsFor({ state: "review_required", publish_step: "publish_called" }, true)).toEqual([
      "resolve_posted",
      "retry",
      "resolve_cancel",
    ]);
    // Post again re-mints the publish job, so it needs the API to publish.
    expect(actionsFor({ state: "review_required", publish_step: "publish_called" }, false)).toEqual([
      "resolve_posted",
      "resolve_cancel",
    ]);
    // It posted needs a publish call to confirm: before that rung the port
    // can only refuse it, so the button is not offered (the row's step says).
    expect(actionsFor({ state: "review_required", publish_step: "container_ready" }, true)).toEqual([
      "retry",
      "resolve_cancel",
    ]);
    expect(actionsFor({ state: "review_required" }, true)).toEqual(["retry", "resolve_cancel"]);
    // A cancel already asked for is honoured by Give up alone.
    expect(
      actionsFor(
        { state: "review_required", cancel_requested: true, publish_step: "publish_called" },
        true,
      ),
    ).toEqual([
      "resolve_cancel",
    ]);
  });

  it("offers nothing on every other state — those rows are the ledger's read-only view", () => {
    for (const state of INTENT_STATES) {
      if (state === "awaiting_approval" || state === "review_required") continue;
      expect(actionsFor({ state }, true), state).toEqual([]);
      expect(actionsFor({ state }, false), state).toEqual([]);
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
    expect(requestFor("cancel", intent)).toEqual({
      command: "cancel",
      body: { intent_id: intent.id },
    });
    for (const action of QUEUE_ACTIONS) {
      expect(ACTION_LABELS[action], action).toBeTruthy();
      // Reschedule, Edit series and Stop repeating are keyed per submission
      // and sent by their own client (`submitRescheduleItem`), so they have no
      // intent-keyed request.
      if (action === "reschedule" || action === "edit_series" || action === "stop_repeating") {
        continue;
      }
      expect(isQueueCommand(requestFor(action, intent).command), action).toBe(true);
    }
  });

  it("offers a planned story still in scheduled its two levers, and a cadence one none", () => {
    expect(actionsFor({ state: "scheduled", origin: "planned" }, true)).toEqual(["reschedule", "cancel"]);
    expect(actionsFor({ state: "scheduled", origin: "planned" }, false)).toEqual(["reschedule", "cancel"]);
    expect(actionsFor({ state: "scheduled", origin: "cadence" }, true)).toEqual([]);
    // Once its cancel is asked for, nothing is left to press until the reaper closes it.
    expect(
      actionsFor({ state: "scheduled", origin: "planned", cancel_requested: true }, true),
    ).toEqual([]);
    // Past `scheduled` the port moves it no longer; its origin changes nothing.
    expect(actionsFor({ state: "prompt_pending", origin: "planned" }, true)).toEqual([]);
    expect(actionsFor({ state: "awaiting_approval", origin: "planned" }, true)).toEqual([
      "approve",
      "mark_posted",
      "skip",
      "reject",
    ]);
  });

  it("offers a repeating story Edit series in Reschedule's place, and Stop repeating while it lives (#485)", () => {
    const repeating = { origin: "planned", repeat: WEEKLY } as const;
    expect(actionsFor({ state: "scheduled", ...repeating }, true)).toEqual([
      "edit_series",
      "cancel",
      "stop_repeating",
    ]);
    expect(actionsFor({ state: "awaiting_approval", ...repeating }, true)).toEqual([
      "approve",
      "mark_posted",
      "skip",
      "reject",
      "stop_repeating",
    ]);
    expect(actionsFor({ state: "review_required", ...repeating }, false)).toEqual([
      "resolve_cancel",
      "stop_repeating",
    ]);
    for (const state of ["prompt_pending", "approved", "publishing", "publishing_ambiguous"] as const) {
      expect(actionsFor({ state, ...repeating }, true), state).toEqual(["stop_repeating"]);
    }
    // A story whose cancel is landing can still stop its series; one that has ended cannot.
    expect(actionsFor({ state: "scheduled", ...repeating, cancel_requested: true }, true)).toEqual([
      "stop_repeating",
    ]);
    expect(actionsFor({ state: "posted", ...repeating }, true)).toEqual([]);
  });

  it("sends Cancel's series answer as cancel with stop_repeat (#485)", () => {
    const intent = { id: "0b6e5f1a-2f4d-4c1e-9a3b-7d8e9f0a1b2c", entered_state_at: "2026-09-12T10:00:00+00:00" };
    expect(requestFor("cancel_series", intent)).toEqual({
      command: "cancel",
      body: { intent_id: intent.id, stop_repeat: true },
    });
  });

  it("knows the non-terminal states the page lists", () => {
    // A hand-typed second opinion on the ORDER, kept deliberately: the
    // membership question is answered against the API's own tuple by
    // `intent-states-contract.test.ts`, which also pins the `?state=` query
    // spellings as `join(",")` of these. This case fails if someone reorders
    // the array, which the query strings would otherwise absorb silently.
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
  it("admits exactly the six commands the queue fronts and nothing else the vocabulary knows", () => {
    expect(QUEUE_COMMANDS).toEqual([
      "approve",
      "mark_posted",
      "skip",
      "reject",
      "resolve_review",
      "cancel",
    ]);
    for (const c of QUEUE_COMMANDS) expect(isQueueCommand(c), c).toBe(true);
    // Real vocabulary names that the queue must NOT forward keyed on the
    // intent: `autopost_now` is unbuilt (501), and `reschedule_item` is keyed
    // per submission, sent by its own client.
    for (const c of ["autopost_now", "reschedule_item", "settings_change", "", " approve", "APPROVE", undefined, 42]) {
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

  it("sends a not_connected refusal to the Accounts tab, where Instagram is connected", () => {
    // The Instagram connect control is the Accounts tab's
    // (`components/dashboard/settings/accounts-tab.tsx`, "Connect Instagram");
    // Integrations holds the Drive folders and the Telegram link.
    expect(refusalCopy("not_connected")).toMatch(/Settings › Accounts/);
    expect(refusalCopy("not_connected")).not.toMatch(/Integrations/);
  });

  it("turns the matrix's normal 409 answers into a sentence, never a raw code", () => {
    expect(refusalCopy("illegal_transition")).toMatch(/already/i);
    expect(refusalCopy("manual_mode")).toMatch(/Posted myself/);
    expect(refusalCopy("not_found")).toMatch(/no longer/i);
  });

  it("separates the person's session from the router being down", () => {
    // #1140: the DISTINCTION this test is named for is the property worth
    // pinning, so it is asserted directly: the two branches must not render
    // the same sentence. The session branch's own wording is pinned once,
    // against a shared source, in `refusal-copy.test.ts`.
    expect(refusalCopy("unauthenticated")).not.toBe(
      refusalCopy("target_router_unreachable"),
    );
    expect(refusalCopy("unauthenticated")).toMatch(/not signed in/i);
    expect(refusalCopy("target_router_unreachable")).toMatch(/Storydump/);
  });

  it("answers the browser's `unreachable` the same as the server's", () => {
    expect(refusalCopy("unreachable")).toBe(refusalCopy("target_router_unreachable"));
    expect(refusalCopy("unreachable")).toContain("Nothing changed");
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

describe("the by-line of a story a rule approved (#1477)", () => {
  it("names each rule as the card does, from the one label map", () => {
    // Literal, on purpose: rebuilding the phrase from RULE_LABELS would agree
    // with a drift in it. `wire-contract.test.ts` pins the map to the API's.
    expect(approvedBy({ approval_rule: "auto" })).toBe("Approved by YOLO");
    expect(approvedBy({ approval_rule: "item" })).toBe("Approved by Auto-approve");
    expect(approvedBy({ approval_rule: "returning" })).toBe("Approved by Returning items");
  });

  it("adds nothing for a person's approval, or a story nobody has approved", () => {
    expect(approvedBy({ approval_rule: null })).toBeNull();
  });

  it("shows a rule this tier does not know as itself, never as nothing", () => {
    // `vocabulary.rule_label`'s rule: a rule the API adds is still named.
    const unknown = { approval_rule: "weekend" } as unknown as Parameters<typeof approvedBy>[0];
    expect(approvedBy(unknown)).toBe("Approved by weekend");
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

describe("a story's place in the Queue (#485)", () => {
  it("is the Planned view at the anchor of the item's live story on its account", () => {
    expect(storyAnchor("m1", "a1")).toBe("story-m1-a1");
    expect(plannedStoryHref("m1", "a1")).toBe("/dashboard/queue?origin=planned#story-m1-a1");
  });
});
