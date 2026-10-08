import type { ContentCalendar } from "@/components/dashboard/media/content-calendar";
import type { Intent } from "@/lib/intents";
import type { FinishedStory } from "./fixtures";

/** The calendar's three lanes, typed from the component, so its shape cannot drift. */
export type CalendarLanes = Parameters<typeof ContentCalendar>[0];

/**
 * The sample's calendar lanes, drawn as the real Calendar draws a workspace
 * (#1649):
 *
 *  - Posted: the month's finished stories that posted, and each story the
 *    visitor marked Posted myself. Each sits in its slot's day, so a story
 *    keeps its cell and changes its lane;
 *  - In Queue: a story waiting for a tap, under its file name, in its slot,
 *    as the Queue lists it;
 *  - Predicted: a scheduled story, labelled by its folder, as the real
 *    Calendar's predicted lane holds the slot plan's scheduled stories;
 *  - a skipped or rejected story leaves the calendar.
 *
 * Each story is drawn once. The real Calendar's queue read also returns its
 * scheduled stories, which it then draws in both lanes.
 */
export function demoCalendarLanes(queue: Intent[], history: FinishedStory[]): CalendarLanes {
  const category = (i: { category: string | null }) => i.category ?? "uncategorised";
  return {
    history: [...queue, ...history]
      .filter((i) => i.state === "posted")
      .map((i) => ({
        posted_at: i.schedule_slot_at,
        media_name: i.file_name,
        category: category(i),
        status: i.state,
      })),
    queue: queue
      .filter((i) => i.state === "awaiting_approval")
      .map((i) => ({
        scheduled_for: i.schedule_slot_at,
        media_name: i.file_name,
        category: category(i),
        status: i.state,
        planned: i.origin === "planned",
      })),
    schedule: queue
      .filter((i) => i.state === "scheduled")
      .map((i) => ({
        slot_time: i.schedule_slot_at,
        predicted_category: i.category,
      })),
  };
}
