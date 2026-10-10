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
  const lanes = demoCalendarLanes(
    [
      story("waiting.jpg", "awaiting_approval"),
      story("later.jpg", "scheduled"),
      story("approved.jpg", "approved"),
      story("skipped.jpg", "skipped"),
      story("rejected.jpg", "rejected"),
    ],
    [
      story("posted.jpg", "posted"),
      story("old-skip.jpg", "skipped"),
      story("old-reject.jpg", "rejected"),
    ],
  );

  it("predicts a story still undecided, by its folder", () => {
    expect(lanes.schedule).toEqual([
      { slot_time: "2026-10-15T14:00:00.000Z", predicted_category: "Memes" },
      { slot_time: "2026-10-15T14:00:00.000Z", predicted_category: "Memes" },
    ]);
  });

  it("queues an approved story under its own name, in its slot", () => {
    expect(lanes.queue).toEqual([
      {
        scheduled_for: "2026-10-15T14:00:00.000Z",
        media_name: "approved.jpg",
        category: "Memes",
        status: "approved",
        planned: false,
      },
    ]);
  });

  it("drops a skipped or rejected story, and draws only what posted under Posted", () => {
    const posted = lanes.history.flatMap((day) => day.newest.map((n) => n.file_name));
    const named = [...lanes.queue.map((i) => i.media_name), ...posted];
    expect(named).not.toContain("skipped.jpg");
    expect(named).not.toContain("rejected.jpg");
    expect(posted).toEqual(["posted.jpg"]);
  });

  it("groups what posted by its day, as the real month read does (#1634)", () => {
    expect(lanes.history).toEqual([
      {
        date: "2026-10-15",
        count: 1,
        newest: [
          {
            id: "posted.jpg",
            state: "posted",
            schedule_slot_at: "2026-10-15T14:00:00.000Z",
            file_name: "posted.jpg",
            category: "Memes",
          },
        ],
      },
    ]);
  });

  it("draws the month the visitor is in", () => {
    expect(demoCalendarLanes([], [], new Date("2026-10-02T12:00:00Z")).month).toEqual({
      year: 2026,
      month: 10,
    });
  });
});
