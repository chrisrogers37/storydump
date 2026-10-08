import { describe, expect, it } from "vitest";
import type { Intent, IntentState } from "@/lib/intents";
import {
  DECISIONS_BEFORE_END,
  demoActionsFor,
  demoReducer,
  endPanelDue,
  initialDemoState,
  type DemoState,
} from "./state";

function story(id: string, state: IntentState = "awaiting_approval"): Intent {
  return {
    id,
    state,
    ig_account_id: "sample-account",
    media_item_id: `${id}-media`,
    schedule_slot_at: "2026-10-15T14:00:00.000Z",
    approval_mode: "manual",
    published_via: null,
    publish_step: null,
    cancel_requested: false,
    ig_permalink: null,
    entered_state_at: "2026-10-15T14:01:00.000Z",
    created_at: "2026-10-14T14:00:00.000Z",
    origin: "cadence",
    scheduled_by_user_id: null,
    scheduled_by: null,
    tz: "UTC",
    miss_reason: null,
    link_url: null,
    file_name: `${id}.jpg`,
    media_kind: "image",
    has_thumbnail: false,
    thumbnail_version: "0123456789abcdef",
    caption: null,
    category: "Product shots",
    account_handle: "example.brand",
    account_display_name: "Example Co",
  };
}

const start = () =>
  initialDemoState([story("a"), story("b"), story("c"), story("d"), story("later", "scheduled")]);

const decide = (state: DemoState, id: string, action: "approve" | "skip" | "reject") =>
  demoReducer(state, { type: "act", intentId: id, action });

describe("a decision in the sample", () => {
  it("is offered as Approve, Skip and Reject on a story waiting on one, and not otherwise", () => {
    expect(demoActionsFor(story("a"))).toEqual(["approve", "skip", "reject"]);
    expect(demoActionsFor(story("a", "scheduled"))).toEqual([]);
    expect(demoActionsFor(story("a", "approved"))).toEqual([]);
  });

  it("approves: the story is approved, and the line says it would post right away", () => {
    const next = decide(start(), "a", "approve");
    expect(next.queue.find((i) => i.id === "a")?.state).toBe("approved");
    expect(next.outcomes).toEqual({
      a: "Approved. In your workspace, this would post to example.brand's Story right away.",
    });
  });

  it("skips and rejects in the product's own words", () => {
    const next = decide(decide(start(), "a", "skip"), "b", "reject");
    expect(next.queue.map((i) => i.state)).toEqual([
      "skipped",
      "rejected",
      "awaiting_approval",
      "awaiting_approval",
      "scheduled",
    ]);
    expect(next.outcomes).toEqual({
      a: "Skipped. In your workspace, this would come back later.",
      b: "Rejected. In your workspace, this would never be offered again for example.brand.",
    });
  });

  it("is made once: a second tap on a decided story changes nothing", () => {
    const once = decide(start(), "a", "approve");
    expect(decide(once, "a", "reject")).toBe(once);
  });

  it("takes no lever the sample does not offer, and no story it does not have", () => {
    const state = start();
    expect(demoReducer(state, { type: "act", intentId: "a", action: "mark_posted" })).toBe(state);
    expect(decide(state, "later", "approve")).toBe(state);
    expect(decide(state, "missing", "approve")).toBe(state);
  });
});

describe("the end panel", () => {
  it(`waits for ${DECISIONS_BEFORE_END} decisions`, () => {
    const two = decide(decide(start(), "a", "approve"), "b", "skip");
    expect(endPanelDue(two)).toBe(false);
    expect(endPanelDue(decide(two, "c", "reject"))).toBe(true);
  });

  it("or for all three pages, each counted once", () => {
    let state = start();
    for (const page of ["overview", "queue", "queue", "overview"] as const) {
      state = demoReducer(state, { type: "visit", page });
    }
    expect(state.visited).toEqual(["overview", "queue"]);
    expect(endPanelDue(state)).toBe(false);
    expect(endPanelDue(demoReducer(state, { type: "visit", page: "calendar" }))).toBe(true);
  });

  it("stays closed once closed", () => {
    let state = start();
    for (const id of ["a", "b", "c"]) state = decide(state, id, "approve");
    state = demoReducer(state, { type: "dismiss" });
    expect(endPanelDue(state)).toBe(false);
    expect(endPanelDue(decide(state, "d", "approve"))).toBe(false);
  });
});
