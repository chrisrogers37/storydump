import { describe, expect, it } from "vitest";
import type { Intent, IntentState } from "@/lib/intents";
import { demoCalendarLanes } from "./calendar";

function story(file: string, state: IntentState): Intent {
  return {
    id: file,
    state,
    ig_account_id: "sample-account",
    media_item_id: `${file}-media`,
    schedule_slot_at: "2026-10-15T14:00:00.000Z",
    approval_mode: "manual",
    published_via: null,
    publish_step: null,
    cancel_requested: false,
    ig_permalink: null,
    entered_state_at: "2026-10-14T18:00:00.000Z",
    created_at: "2026-10-13T14:00:00.000Z",
    origin: "cadence",
    scheduled_by_user_id: null,
    scheduled_by: null,
    tz: "UTC",
    miss_reason: null,
    link_url: null,
    file_name: file,
    media_kind: "image",
    has_thumbnail: false,
    thumbnail_version: "0123456789abcdef",
    caption: null,
    category: "Memes",
    account_handle: "example.brand",
    account_display_name: "Example Co",
  };
}

describe("the sample's calendar lanes", () => {
  /** Marked Posted myself by the visitor, a day after its slot. */
  const marked = { ...story("marked.jpg", "posted"), entered_state_at: "2026-10-16T09:30:00.000Z" };
  const lanes = demoCalendarLanes(
    [
      story("waiting.jpg", "awaiting_approval"),
      story("later.jpg", "scheduled"),
      marked,
      story("skipped.jpg", "skipped"),
      story("rejected.jpg", "rejected"),
    ],
    [
      story("posted.jpg", "posted"),
      story("old-skip.jpg", "skipped"),
      story("old-reject.jpg", "rejected"),
    ],
  );

  it("queues a story waiting for a tap under its own name, in its slot, as the Queue lists it", () => {
    expect(lanes.queue).toEqual([
      {
        scheduled_for: "2026-10-15T14:00:00.000Z",
        media_name: "waiting.jpg",
        category: "Memes",
        status: "awaiting_approval",
        planned: false,
      },
    ]);
  });

  it("predicts a scheduled story by its folder, as the real Calendar's predicted lane does", () => {
    expect(lanes.schedule).toEqual([
      { slot_time: "2026-10-15T14:00:00.000Z", predicted_category: "Memes" },
    ]);
  });

  it("draws a story marked posted under Posted, in its slot's day, beside the month's posts", () => {
    // A story keeps its cell: marking it posted a day late changes its lane, not its day.
    expect(lanes.history).toEqual([
      { posted_at: "2026-10-15T14:00:00.000Z", media_name: "marked.jpg", category: "Memes", status: "posted" },
      { posted_at: "2026-10-15T14:00:00.000Z", media_name: "posted.jpg", category: "Memes", status: "posted" },
    ]);
  });

  it("draws each story once, and drops one that was skipped or rejected", () => {
    const named = [...lanes.queue.map((i) => i.media_name), ...lanes.history.map((i) => i.media_name)];
    for (const gone of ["skipped.jpg", "rejected.jpg", "old-skip.jpg", "old-reject.jpg"]) {
      expect(named).not.toContain(gone);
    }
    // Scheduled is Predicted only, never In Queue as well.
    expect(named).not.toContain("later.jpg");
    expect(new Set(named).size).toBe(named.length);
  });
});
