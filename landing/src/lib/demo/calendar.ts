import type { ContentCalendar } from "@/components/dashboard/media/content-calendar";
import { monthOf, NAMES_PER_DAY, type IntentDay } from "@/lib/calendar-month";
import type { Intent } from "@/lib/intents";
import { dateInZone } from "@/lib/zoned-dates";

/** The calendar's lanes, typed from the component, so its shape cannot drift. */
export type CalendarLanes = Parameters<typeof ContentCalendar>[0];

/** The sample's zone: it draws in UTC, as the calendar does without one. */
const SAMPLE_TZ = "UTC";

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
 * lane carries posted stories only, grouped by day as the real month read
 * groups them (#1634): `ContentCalendar` draws that whole lane under its
 * "Posted" legend, and nothing a visitor does here may look like it posted.
 * The sample draws this month, with no month or day to navigate to.
 */
export function demoCalendarLanes(
  queue: Intent[],
  history: Intent[],
  now: Date = new Date(),
): CalendarLanes {
  const category = (i: Intent) => i.category ?? "uncategorised";
  const days = new Map<string, IntentDay>();
  const newestFirst = history
    .filter((i) => i.state === "posted")
    .sort((a, b) => Date.parse(b.schedule_slot_at) - Date.parse(a.schedule_slot_at));
  for (const i of newestFirst) {
    const date = dateInZone(i.schedule_slot_at, SAMPLE_TZ);
    const day: IntentDay = days.get(date) ?? { date, count: 0, newest: [] };
    day.count += 1;
    if (day.newest.length < NAMES_PER_DAY) {
      day.newest.push({
        id: i.id,
        state: i.state,
        schedule_slot_at: i.schedule_slot_at,
        file_name: i.file_name,
        category: i.category,
      });
    }
    days.set(date, day);
  }
  return {
    month: monthOf(dateInZone(now, SAMPLE_TZ)),
    history: [...days.values()],
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
