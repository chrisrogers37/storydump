"use client";

import { useMemo } from "react";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { dateInZone, formatCalendarDate } from "@/lib/zoned-dates";
import { TONE_CLASS, TONE_DOT, type BadgeTone } from "@/components/dashboard/tone";

interface HistoryItem {
  posted_at: string;
  media_name: string;
  category: string;
  status: string;
}

interface ScheduleSlot {
  slot_time: string;
  predicted_category: string | null;
}

interface QueueItem {
  scheduled_for: string;
  media_name: string;
  category: string;
  status: string;
}

interface CalendarDay {
  date: string;
  dayOfMonth: number;
  isToday: boolean;
  isCurrentMonth: boolean;
  posts: { label: string; category: string; type: "past" | "queued" | "predicted" }[];
}

/** A UTC calendar date as `YYYY-MM-DD`. */
const isoDay = (d: Date) => d.toISOString().slice(0, 10);

/**
 * The month on the workspace's own calendar (#1511). Today, the month shown
 * and the day each item falls on are all read in `tz`, the zone the Queue's
 * times already use. Read on whatever clock the code ran on, the server (UTC)
 * and an evening browser in New York disagreed about which day was today and
 * which day a slot fell on. The grid itself is calendar arithmetic on UTC
 * dates, which has no zone to disagree about.
 */
export function buildCalendarDays(
  history: HistoryItem[],
  queue: QueueItem[],
  schedule: ScheduleSlot[],
  tz: string,
  now: Date = new Date()
): { month: string; days: CalendarDay[] } {
  const today = dateInZone(now, tz);
  const [year, month] = today.split("-").map(Number);

  // The month's first and last days, padded out to whole Monday-first weeks.
  const first = new Date(Date.UTC(year, month - 1, 1));
  const last = new Date(Date.UTC(year, month, 0));
  const start = new Date(first);
  start.setUTCDate(start.getUTCDate() - ((first.getUTCDay() + 6) % 7));
  const end = new Date(last);
  end.setUTCDate(end.getUTCDate() + ((7 - last.getUTCDay()) % 7));

  // Index events by the workspace's day.
  const postsByDate = new Map<string, CalendarDay["posts"]>();
  const add = (instant: string, post: CalendarDay["posts"][number]) => {
    const date = dateInZone(instant, tz);
    if (!postsByDate.has(date)) postsByDate.set(date, []);
    postsByDate.get(date)!.push(post);
  };

  for (const item of history) {
    add(item.posted_at, { label: item.media_name, category: item.category, type: "past" });
  }
  for (const item of queue) {
    add(item.scheduled_for, { label: item.media_name, category: item.category, type: "queued" });
  }
  for (const slot of schedule) {
    add(slot.slot_time, {
      label: slot.predicted_category || "any",
      category: slot.predicted_category || "any",
      type: "predicted",
    });
  }

  const days: CalendarDay[] = [];
  for (const current = new Date(start); current <= end; current.setUTCDate(current.getUTCDate() + 1)) {
    const date = isoDay(current);
    days.push({
      date,
      dayOfMonth: current.getUTCDate(),
      isToday: date === today,
      isCurrentMonth: current.getUTCMonth() === month - 1,
      posts: postsByDate.get(date) || [],
    });
  }

  return {
    month: formatCalendarDate(isoDay(first), { month: "long", year: "numeric" }),
    days,
  };
}

const typeTone = {
  past: "active",
  queued: "progress",
  predicted: "inert",
} as const satisfies Record<string, BadgeTone>;

export function ContentCalendar({
  history,
  queue,
  schedule,
  tz = "UTC",
}: {
  history: HistoryItem[];
  queue: QueueItem[];
  schedule: ScheduleSlot[];
  /**
   * The workspace's zone, which today, the month and each item's day are read
   * in. Without one it reads UTC, the zone the API writes its timestamps in.
   */
  tz?: string;
}) {
  const { month: monthName, days } = useMemo(
    () => buildCalendarDays(history, queue, schedule, tz),
    [history, queue, schedule, tz]
  );

  const weekDays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle>{monthName}</CardTitle>
        <div className="flex gap-4 text-xs text-muted-foreground">
          <span className="flex items-center gap-1">
            <span className={cn("h-2 w-2 rounded-full", TONE_DOT[typeTone.past])} /> Posted
          </span>
          <span className="flex items-center gap-1">
            <span className={cn("h-2 w-2 rounded-full", TONE_DOT[typeTone.queued])} /> In Queue
          </span>
          <span className="flex items-center gap-1">
            <span className={cn("h-2 w-2 rounded-full", TONE_DOT[typeTone.predicted])} /> Predicted
          </span>
        </div>
      </CardHeader>
      <CardContent>
        {/* Header */}
        <div className="grid grid-cols-7 gap-px mb-1">
          {weekDays.map((d) => (
            <div
              key={d}
              className="text-center text-xs font-medium text-muted-foreground py-1"
            >
              {d}
            </div>
          ))}
        </div>

        {/* Days grid */}
        <div className="grid grid-cols-7 gap-px">
          {days.map((day) => (
            <div
              key={day.date}
              className={cn(
                "min-h-[80px] border rounded-sm p-1",
                !day.isCurrentMonth && "opacity-30",
                day.isToday && "border-primary"
              )}
            >
              <span
                className={cn(
                  "text-xs font-medium",
                  day.isToday && "text-primary"
                )}
              >
                {day.dayOfMonth}
              </span>
              <div className="mt-0.5 space-y-0.5">
                {day.posts.slice(0, 3).map((post, i) => (
                  <div
                    key={i}
                    className={cn(
                      "truncate rounded px-1 py-0.5 text-[10px]",
                      TONE_CLASS[typeTone[post.type]]
                    )}
                    title={post.label}
                  >
                    {post.label}
                  </div>
                ))}
                {day.posts.length > 3 && (
                  <div className="text-[10px] text-muted-foreground px-1">
                    +{day.posts.length - 3} more
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}
