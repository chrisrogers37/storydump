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
  /** Scheduled for the next day's first slot, so its lane cannot be mistaken for the waiting story's. */
  const later = { ...story("later.jpg", "scheduled"), schedule_slot_at: "2026-10-16T08:00:00.000Z" };
  const lanes = demoCalendarLanes(
    [
      story("waiting.jpg", "awaiting_approval"),
      later,
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

  it("queues the stories waiting for a tap and the ones scheduled, each under its own name, in its slot, as the Queue lists them", () => {
    expect(lanes.queue).toEqual([
      {
        scheduled_for: "2026-10-15T14:00:00.000Z",
        media_name: "waiting.jpg",
        category: "Memes",
        status: "awaiting_approval",
        planned: false,
      },
      {
        scheduled_for: "2026-10-16T08:00:00.000Z",
        media_name: "later.jpg",
        category: "Memes",
        status: "scheduled",
        planned: false,
      },
    ]);
  });

  it("predicts nothing: every slot the sample plans already holds a story its Queue names (#1634)", () => {
    expect(lanes.predicted).toEqual([]);
  });

  it("groups what posted by its slot's day, as the real month read does (#1634): a marked story beside the month's posts", () => {
    // A story keeps its cell: marking it posted a day late changes its lane, not its day.
    const named = (file: string) => ({
      id: file,
      state: "posted",
      schedule_slot_at: "2026-10-15T14:00:00.000Z",
      file_name: file,
      category: "Memes",
    });
    expect(lanes.history).toEqual([
      { date: "2026-10-15", count: 2, newest: [named("marked.jpg"), named("posted.jpg")] },
    ]);
  });

  it("counts every post of a day, and names the newest three", () => {
    // The month read's own shape: a count, and at most its `per_day` names.
    const hours = ["09", "11", "13", "15", "17"];
    const posts = hours.map((hour) => ({
      ...story(`posted-${hour}.jpg`, "posted"),
      schedule_slot_at: `2026-10-15T${hour}:00:00.000Z`,
    }));
    const [day] = demoCalendarLanes([], posts).history;
    expect(day.count).toBe(5);
    expect(day.newest.map((n) => n.file_name)).toEqual([
      "posted-17.jpg",
      "posted-15.jpg",
      "posted-13.jpg",
    ]);
  });

  it("draws each story once, and drops one that was skipped or rejected", () => {
    const posted = lanes.history.flatMap((day) => day.newest.map((n) => n.file_name));
    const named = [...lanes.queue.map((i) => i.media_name), ...posted];
    for (const gone of ["skipped.jpg", "rejected.jpg", "old-skip.jpg", "old-reject.jpg"]) {
      expect(named).not.toContain(gone);
    }
    // A scheduled story is In Queue under its name, as the Queue lists it.
    expect(named).toContain("later.jpg");
    expect(new Set(named).size).toBe(named.length);
  });

  it("draws the month the visitor is in, on the sample's own clock", () => {
    expect(demoCalendarLanes([], [], new Date("2026-10-02T12:00:00Z")).month).toEqual({
      year: 2026,
      month: 10,
    });
    // Half past midnight in London on 1 October is still September in UTC.
    expect(demoCalendarLanes([], [], new Date("2026-09-30T23:30:00Z")).month).toEqual({
      year: 2026,
      month: 10,
    });
  });
});
