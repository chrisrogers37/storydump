import type { ContentCalendar } from "@/components/dashboard/media/content-calendar";
import { monthOf, NAMES_PER_DAY, predictedDays, type IntentDay } from "@/lib/calendar-month";
import type { Intent } from "@/lib/intents";
import { dateInZone } from "@/lib/zoned-dates";
import { SAMPLE_TZ, type FinishedStory } from "./fixtures";

/** The calendar's lanes, typed from the component, so its shape cannot drift. */
export type CalendarLanes = Parameters<typeof ContentCalendar>[0];

/**
 * The sample's calendar lanes, drawn as the real Calendar draws a workspace
 * (#1649):
 *
 *  - Posted: the month's finished stories that posted, and each story the
 *    visitor marked Posted myself, grouped by the day of each one's slot, as
 *    the real month read groups them (#1634). So a story keeps its cell and
 *    changes its lane;
 *  - In Queue: a story waiting for a tap, under its file name, in its slot,
 *    as the Queue lists it;
 *  - Predicted: the scheduled stories, counted on their days and labelled as
 *    predicted, as the real Calendar counts the slots its cadence will open
 *    (#1634). The sample's slot plan is a fixture, so its next slots already
 *    hold stories, where a real cadence mints one only as its slot comes due;
 *  - a skipped or rejected story leaves the calendar.
 *
 * Each story is drawn once. The sample draws this month on its own clock, with
 * no month or day to navigate to.
 */
export function demoCalendarLanes(
  queue: Intent[],
  history: FinishedStory[],
  now: Date = new Date(),
): CalendarLanes {
  const days = new Map<string, IntentDay>();
  const newestFirst = [...queue, ...history]
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
      .filter((i) => i.state === "awaiting_approval")
      .map((i) => ({
        scheduled_for: i.schedule_slot_at,
        media_name: i.file_name,
        category: i.category ?? "uncategorised",
        status: i.state,
        planned: i.origin === "planned",
      })),
    predicted: predictedDays(
      queue
        .filter((i) => i.state === "scheduled")
        .map((i) => ({ day: dateInZone(i.schedule_slot_at, SAMPLE_TZ) })),
    ),
  };
}
