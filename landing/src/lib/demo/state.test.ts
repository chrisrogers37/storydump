import { describe, expect, it } from "vitest";
import { actionsFor, type Intent, type IntentState, type QueueAction } from "@/lib/intents";
import { SAMPLE_API_PUBLISHING, sampleWorkspace } from "./fixtures";
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

/** When the visitor tapped. */
const AT = "2026-10-15T15:30:00.000Z";

const decide = (state: DemoState, id: string, action: QueueAction) =>
  demoReducer(state, { type: "act", intentId: id, action, at: AT });

describe("a decision in the sample", () => {
  it("is offered as a new workspace's Queue offers it: Posted myself, Skip and Reject, and not otherwise", () => {
    // Direct posting is off in a new workspace, so there is no Approve (#1649).
    expect(demoActionsFor(story("a"))).toEqual(["mark_posted", "skip", "reject"]);
    expect(demoActionsFor(story("a", "scheduled"))).toEqual([]);
    expect(demoActionsFor(story("a", "posted"))).toEqual([]);
  });

  it("hides no lever the real Queue would offer the sample's stories", () => {
    // `demoActionsFor` keeps the levers the sample answers. Should the Queue's
    // matrix grow one for these stories, this fails rather than the sample
    // quietly offering less than a new workspace does.
    for (const queued of sampleWorkspace(new Date("2026-10-15T16:20:00.000Z")).queue) {
      expect(demoActionsFor(queued), queued.file_name).toEqual(
        actionsFor(queued.state, SAMPLE_API_PUBLISHING),
      );
    }
  });

  it("marks a story posted from the moment of the tap, and the line says what that records", () => {
    const next = decide(start(), "a", "mark_posted");
    const a = next.queue.find((i) => i.id === "a");
    expect(a?.state).toBe("posted");
    expect(a?.entered_state_at).toBe(AT);
    expect(next.outcomes).toEqual({
      a: "Marked as posted. In your workspace, this would record that you posted it to example.brand's Story yourself.",
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
    const once = decide(start(), "a", "mark_posted");
    expect(decide(once, "a", "reject")).toBe(once);
  });

  it("takes no lever the sample does not offer, and no story it does not have", () => {
    const state = start();
    expect(decide(state, "a", "approve")).toBe(state);
    expect(decide(state, "later", "mark_posted")).toBe(state);
    expect(decide(state, "missing", "mark_posted")).toBe(state);
  });
});

describe("the end panel", () => {
  it(`waits for ${DECISIONS_BEFORE_END} decisions, because its line is about the taps`, () => {
    // Nothing else brings it up: opening the three pages once did, and its
    // "That's the job: one tap per Story." spoke of taps never made (#1649).
    expect(endPanelDue(start())).toBe(false);
    const two = decide(decide(start(), "a", "mark_posted"), "b", "skip");
    expect(endPanelDue(two)).toBe(false);
    expect(endPanelDue(decide(two, "c", "reject"))).toBe(true);
  });

  it("stays closed once closed", () => {
    let state = start();
    for (const id of ["a", "b", "c"]) state = decide(state, id, "mark_posted");
    state = demoReducer(state, { type: "dismiss" });
    expect(endPanelDue(state)).toBe(false);
    expect(endPanelDue(decide(state, "d", "mark_posted"))).toBe(false);
  });
});
