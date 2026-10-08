"use client";

import Link from "next/link";
import { useMemo } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { dateInZone, formatCalendarDate } from "@/lib/zoned-dates";
import {
  monthGrid,
  monthParam,
  shiftMonth,
  type IntentDay,
  type Month,
} from "@/lib/calendar-month";
import { TONE_CLASS, TONE_DOT, type BadgeTone } from "@/components/dashboard/tone";

interface ScheduleSlot {
  slot_time: string;
  predicted_category: string | null;
}

interface QueueItem {
  scheduled_for: string;
  media_name: string;
  category: string;
  status: string;
  /** A story a person planned for a chosen time (#1413), not one the slot plan placed. */
  planned: boolean;
}

interface CalendarDay {
  date: string;
  dayOfMonth: number;
  isToday: boolean;
  isCurrentMonth: boolean;
  /** How many stories the day posted: the month read's count (#1634). */
  posted: number;
  /** Posted stories beyond the names the month read carried. */
  postedUnnamed: number;
  posts: {
    label: string;
    category: string;
    type: "past" | "queued" | "planned" | "predicted";
  }[];
}

/** Chips a day draws before "+N more". */
const CHIPS_PER_DAY = 3;

/**
 * The month's grid, read in the workspace's zone (#1511). Today and the day
 * each item falls on are read in `tz`, the zone the Queue's times already
 * use; the month's history arrives already grouped by the workspace's local
 * day (`GET …/intents/days`). The grid itself is calendar arithmetic on UTC
 * dates (`monthGrid`), which has no zone to disagree about.
 */
export function buildCalendarDays(
  month: Month,
  history: IntentDay[],
  queue: QueueItem[],
  schedule: ScheduleSlot[],
  tz: string,
  now: Date = new Date()
): { month: string; days: CalendarDay[] } {
  const today = dateInZone(now, tz);

  const postsByDate = new Map<string, CalendarDay["posts"]>();
  const add = (date: string, post: CalendarDay["posts"][number]) => {
    if (!postsByDate.has(date)) postsByDate.set(date, []);
    postsByDate.get(date)!.push(post);
  };

  const postedByDate = new Map<string, IntentDay>();
  for (const day of history) {
    postedByDate.set(day.date, day);
    for (const item of day.newest) {
      add(day.date, {
        label: item.file_name,
        category: item.category ?? "uncategorised",
        type: "past",
      });
    }
  }
  for (const item of queue) {
    add(dateInZone(item.scheduled_for, tz), {
      label: item.media_name,
      category: item.category,
      type: item.planned ? "planned" : "queued",
    });
  }
  for (const slot of schedule) {
    add(dateInZone(slot.slot_time, tz), {
      label: slot.predicted_category || "any",
      category: slot.predicted_category || "any",
      type: "predicted",
    });
  }

  const days = monthGrid(month).dates.map((date) => {
    const posted = postedByDate.get(date);
    return {
      date,
      dayOfMonth: Number(date.slice(8)),
      isToday: date === today,
      isCurrentMonth: Number(date.slice(5, 7)) === month.month,
      posted: posted?.count ?? 0,
      postedUnnamed: posted ? posted.count - posted.newest.length : 0,
      posts: postsByDate.get(date) || [],
    };
  });

  return {
    month: formatCalendarDate(`${monthParam(month)}-01`, {
      month: "long",
      year: "numeric",
    }),
    days,
  };
}

const typeTone = {
  past: "active",
  queued: "progress",
  predicted: "inert",
} as const satisfies Record<string, BadgeTone>;

/**
 * A planned story is upcoming, as a queued one is, so it keeps the queue's
 * colour; it is drawn as an outline rather than filled, which is what sets it
 * apart. One mark, not a new tone: the badge vocabulary stays as it is.
 */
const PLANNED_CHIP = "ring-1 ring-inset ring-tap text-tap-ink";
const PLANNED_DOT = "ring-1 ring-inset ring-tap";

function chipClass(type: CalendarDay["posts"][number]["type"]): string {
  return type === "planned" ? PLANNED_CHIP : TONE_CLASS[typeTone[type]];
}

function MonthLink({ month, direction }: { month: Month; direction: -1 | 1 }) {
  const target = shiftMonth(month, direction);
  const Icon = direction < 0 ? ChevronLeft : ChevronRight;
  return (
    <Link
      href={`?month=${monthParam(target)}`}
      scroll={false}
      className="rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
      aria-label={direction < 0 ? "Previous month" : "Next month"}
    >
      <Icon className="h-4 w-4" aria-hidden />
    </Link>
  );
}

/**
 * The days a phone lists (#1649 F12): this month's that hold something, in
 * order. Seven columns at 390 px leave a name two to four letters, so a phone
 * reads the month as a list of its days and their names at full width.
 */
export function listedDays(days: CalendarDay[]): CalendarDay[] {
  return days.filter((d) => d.isCurrentMonth && (d.posted > 0 || d.posts.length > 0));
}

/** A day's first chips, and "+N more" for its other items and unnamed posts. */
function DayChips({ day, size }: { day: CalendarDay; size: "cell" | "row" }) {
  const more = Math.max(0, day.posts.length - CHIPS_PER_DAY) + day.postedUnnamed;
  const text = size === "cell" ? "text-[10px]" : "text-xs";
  return (
    <div className={size === "cell" ? "mt-0.5 space-y-0.5" : "mt-1 flex flex-wrap gap-1"}>
      {day.posts.slice(0, CHIPS_PER_DAY).map((post, i) => (
        <div
          key={i}
          className={cn("max-w-full truncate rounded px-1 py-0.5", text, chipClass(post.type))}
          title={post.label}
        >
          {post.type === "planned" && <span className="sr-only">Planned: </span>}
          {post.label}
        </div>
      ))}
      {more > 0 && <div className={cn("px-1 text-muted-foreground", text)}>+{more} more</div>}
    </div>
  );
}

function DayContents({ day }: { day: CalendarDay }) {
  return (
    <>
      <span className={cn("text-xs font-medium", day.isToday && "text-primary")}>
        {day.dayOfMonth}
      </span>
      {day.posted > 0 && (
        <span className="ml-1 text-[10px] text-muted-foreground">{day.posted} posted</span>
      )}
      <DayChips day={day} size="cell" />
    </>
  );
}

/** A listed day on a phone: its date and count on one line, its names below. */
function DayRow({ day }: { day: CalendarDay }) {
  return (
    <>
      <div className="flex items-baseline justify-between gap-2">
        <span className={cn("text-sm font-medium", day.isToday && "text-primary")}>
          {formatCalendarDate(day.date, { weekday: "short", day: "numeric" })}
        </span>
        {day.posted > 0 && (
          <span className="text-xs text-muted-foreground">{day.posted} posted</span>
        )}
      </div>
      <DayChips day={day} size="row" />
    </>
  );
}

export function ContentCalendar({
  month,
  history,
  queue,
  schedule,
  tz = "UTC",
  navigable = false,
  selected = null,
}: {
  /** The month on screen. */
  month: Month;
  /** The month's posted stories, a count and the newest names per local day. */
  history: IntentDay[];
  queue: QueueItem[];
  schedule: ScheduleSlot[];
  /**
   * The workspace's zone, which today, the month and each item's day are read
   * in. Without one it reads UTC, the zone the API writes its timestamps in.
   */
  tz?: string;
  /**
   * Previous and next month, and each day a link to its day view, through
   * `?month=` and `?day=` (#1634). The sample workspace draws without them.
   */
  navigable?: boolean;
  /** The day whose list is open, marked on the grid. */
  selected?: string | null;
}) {
  const { month: monthName, days } = useMemo(
    () => buildCalendarDays(month, history, queue, schedule, tz),
    [month, history, queue, schedule, tz]
  );

  const weekDays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const listed = listedDays(days);
  const dayHref = (date: string) => `?month=${monthParam(month)}&day=${date}`;
  const cellClass = (day: CalendarDay) =>
    cn(
      "block min-h-[80px] border rounded-sm p-1",
      !day.isCurrentMonth && "opacity-30",
      day.isToday && "border-primary",
      day.date === selected && "ring-2 ring-primary"
    );

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center gap-2">
          {navigable && <MonthLink month={month} direction={-1} />}
          <CardTitle>{monthName}</CardTitle>
          {navigable && <MonthLink month={month} direction={1} />}
        </div>
        <div className="flex gap-4 text-xs text-muted-foreground">
          <span className="flex items-center gap-1">
            <span className={cn("h-2 w-2 rounded-full", TONE_DOT[typeTone.past])} /> Posted
          </span>
          <span className="flex items-center gap-1">
            <span className={cn("h-2 w-2 rounded-full", TONE_DOT[typeTone.queued])} /> In Queue
          </span>
          <span className="flex items-center gap-1">
            <span className={cn("h-2 w-2 rounded-full", PLANNED_DOT)} /> Planned
          </span>
          <span className="flex items-center gap-1">
            <span className={cn("h-2 w-2 rounded-full", TONE_DOT[typeTone.predicted])} /> Predicted
          </span>
        </div>
      </CardHeader>
      <CardContent>
        {/* Phones: the days that hold something, as a list (#1649 F12). */}
        <div className="sm:hidden">
          {listed.length === 0 ? (
            <p className="py-2 text-sm text-muted-foreground">
              Nothing on the calendar this month.
            </p>
          ) : (
            <ol className="divide-y">
              {listed.map((day) => (
                <li key={day.date}>
                  {navigable ? (
                    <Link
                      href={dayHref(day.date)}
                      scroll={false}
                      className={cn("block py-2", day.date === selected && "bg-muted")}
                      aria-current={day.date === selected ? "date" : undefined}
                    >
                      <DayRow day={day} />
                    </Link>
                  ) : (
                    <div className="py-2">
                      <DayRow day={day} />
                    </div>
                  )}
                </li>
              ))}
            </ol>
          )}
        </div>

        {/* Wider screens: the month's grid. */}
        <div className="hidden sm:block">
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

          <div className="grid grid-cols-7 gap-px">
            {days.map((day) =>
              navigable ? (
                <Link
                  key={day.date}
                  href={dayHref(day.date)}
                  scroll={false}
                  className={cn(cellClass(day), "hover:bg-muted/50")}
                  aria-label={`${formatCalendarDate(day.date, {
                    weekday: "long",
                    month: "long",
                    day: "numeric",
                  })}, ${day.posted} posted`}
                  aria-current={day.date === selected ? "date" : undefined}
                >
                  <DayContents day={day} />
                </Link>
              ) : (
                <div key={day.date} className={cellClass(day)}>
                  <DayContents day={day} />
                </div>
              )
            )}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
