import type { ContentCalendar } from "@/components/dashboard/media/content-calendar";
import type { Intent } from "@/lib/intents";

/** The calendar's three lanes, typed from the component, so its shape cannot drift. */
export type CalendarLanes = Parameters<typeof ContentCalendar>[0];

/**
 * The sample's calendar lanes (#1480). Deliberately NOT the real calendar
 * page's mapping, which puts every open story in the "In Queue" lane, where
 * an approval would change nothing a visitor can see:
 *
 *  - a story waiting on a decision, or scheduled, is "Predicted", labelled by
 *    its folder: nothing the workspace has agreed to yet;
 *  - an approved story is "In Queue", under its file name;
 *  - a skipped or rejected story leaves the calendar.
 *
 * So Approve, Skip and Reject each change what the calendar draws. The past
 * lane carries posted stories only: `ContentCalendar` draws that whole lane
 * under its "Posted" legend, and nothing a visitor does here may look like
 * it posted.
 */
export function demoCalendarLanes(queue: Intent[], history: Intent[]): CalendarLanes {
  const category = (i: Intent) => i.category ?? "uncategorised";
  return {
    history: history
      .filter((i) => i.state === "posted")
      .map((i) => ({
        posted_at: i.entered_state_at,
        media_name: i.file_name,
        category: category(i),
        status: i.state,
      })),
    queue: queue
      .filter((i) => i.state === "approved")
      .map((i) => ({
        scheduled_for: i.schedule_slot_at,
        media_name: i.file_name,
        category: category(i),
        status: i.state,
        planned: i.origin === "planned",
      })),
    schedule: queue
      .filter((i) => i.state === "awaiting_approval" || i.state === "scheduled")
      .map((i) => ({
        slot_time: i.schedule_slot_at,
        predicted_category: i.category,
      })),
  };
}
