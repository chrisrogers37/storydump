/**
 * The calendar's month and day, as local dates in the workspace's zone (#1634).
 *
 * Every date here is a calendar date, `YYYY-MM-DD`, with no zone attached: the
 * API turns `?from=` and `?to=` into the instants those days start in the
 * workspace's own zone, so the month on screen and the month the API counts
 * are the same days. The arithmetic is on UTC dates, which have no zone to
 * disagree about.
 */

import type { Intent } from "@/lib/intents";
import type { ThumbnailMedia } from "@/lib/thumbnails";

/** A month, `month` 1-12. */
export type Month = { year: number; month: number };

/** One local day of `GET /workspaces/{ws}/intents/days`. */
export type IntentDay = {
  date: string;
  /** Every intent the day holds in the asked states: a COUNT, not a list's length. */
  count: number;
  /**
   * The day's newest, at most the read's `per_day`. The read carries each
   * one's thumbnail fields (#1634); the sample workspace's days carry none,
   * so their chips draw the glyph.
   */
  newest: ({
    id: string;
    state: string;
    schedule_slot_at: string;
    file_name: string;
    category: string | null;
  } & Omit<ThumbnailMedia, "file_name">)[];
};

export type IntentDaysResponse = { days: IntentDay[]; per_day: number };

/** Names each day of the month carries: the chips a calendar cell draws. */
export const NAMES_PER_DAY = 3;

const MONTH = /^(\d{4})-(\d{2})$/;
const DAY = /^\d{4}-\d{2}-\d{2}$/;

const isoDay = (d: Date) => d.toISOString().slice(0, 10);

/**
 * A year the calendar can draw: four digits, as the dates it formats are
 * spelled, and a grid that ends before year 10000.
 */
const drawable = (year: number) => year >= 1000 && year < 9999;

/** `?month=2026-10` → the month, or null when it is not one it can draw. */
export function parseMonth(value: unknown): Month | null {
  const found = typeof value === "string" ? MONTH.exec(value) : null;
  if (!found) return null;
  const year = Number(found[1]);
  const month = Number(found[2]);
  return month >= 1 && month <= 12 && drawable(year) ? { year, month } : null;
}

/** `?day=2026-10-03` → the date, or null when it is not a real one it can draw. */
export function parseDay(value: unknown): string | null {
  if (typeof value !== "string" || !DAY.test(value)) return null;
  if (!drawable(Number(value.slice(0, 4)))) return null;
  const d = new Date(`${value}T00:00:00Z`);
  return !Number.isNaN(d.getTime()) && isoDay(d) === value ? value : null;
}

/** The month a local date falls in. */
export function monthOf(date: string): Month {
  return { year: Number(date.slice(0, 4)), month: Number(date.slice(5, 7)) };
}

/** `{ year: 2026, month: 10 }` → `"2026-10"`, the `?month=` spelling. */
export function monthParam({ year, month }: Month): string {
  return `${year}-${String(month).padStart(2, "0")}`;
}

/** The month *delta* months away: December and one is January. */
export function shiftMonth({ year, month }: Month, delta: number): Month {
  const d = new Date(Date.UTC(year, month - 1 + delta, 1));
  return { year: d.getUTCFullYear(), month: d.getUTCMonth() + 1 };
}

/** The local date *n* days after *date*. */
export function addDays(date: string, n: number): string {
  const d = new Date(`${date}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return isoDay(d);
}

/**
 * The month drawn in whole Monday-first weeks: the grid's dates, and
 * `[from, to)`, the range the page asks the API for. The grid's first and last
 * weeks reach into the months either side, and those days are counted too.
 */
export function monthGrid({ year, month }: Month): {
  from: string;
  to: string;
  dates: string[];
} {
  const first = new Date(Date.UTC(year, month - 1, 1));
  const last = new Date(Date.UTC(year, month, 0));
  const start = new Date(first);
  start.setUTCDate(start.getUTCDate() - ((first.getUTCDay() + 6) % 7));
  const end = new Date(last);
  end.setUTCDate(end.getUTCDate() + ((7 - last.getUTCDay()) % 7));
  const dates: string[] = [];
  for (const d = new Date(start); d <= end; d.setUTCDate(d.getUTCDate() + 1)) {
    dates.push(isoDay(d));
  }
  return { from: dates[0], to: addDays(dates[dates.length - 1], 1), dates };
}

/** A planned story of `GET /workspaces/{ws}/upcoming`: the intents read's row and its day. */
export type PlannedStory = Intent & {
  /** The day its slot falls on in the workspace's zone. */
  day: string;
};

/**
 * A slot the cadence will open, from the same read: a place the cadence will
 * look for a story, not a story, so it has no state, no file and no folder.
 */
export type PredictedSlot = {
  kind: "predicted";
  schedule_slot_at: string;
  /** The day the slot falls on in the workspace's zone. */
  day: string;
  /** The zone its account posts in. The calendar reads every time in the workspace's. */
  tz: string;
  ig_account_id: string;
  account_handle: string | null;
  account_display_name: string | null;
};

/**
 * `GET /workspaces/{ws}/upcoming?from=&to=`: what is coming on the local days
 * `[from, to)`, each list soonest first and cut at its own limit.
 */
export type UpcomingResponse = {
  from: string;
  to: string;
  /** The stories a person planned that are still `scheduled`. */
  planned: PlannedStory[];
  planned_truncated: boolean;
  predicted: PredictedSlot[];
  predicted_truncated: boolean;
};

/** How many slots the cadence will open on a local day: a count, never a list of stories. */
export type PredictedDay = { date: string; count: number };

/** The predicted slots counted on their days, soonest day first. */
export function predictedDays(slots: { day: string }[]): PredictedDay[] {
  const counts = new Map<string, number>();
  for (const { day } of slots) counts.set(day, (counts.get(day) ?? 0) + 1);
  return [...counts].map(([date, count]) => ({ date, count }));
}

/**
 * Where the upcoming read stops vouching for its lists. Each list is cut by
 * count in slot order, so a cut list is whole before its last row's day, may
 * be short on that day, and holds nothing of the days after it. `planned` and
 * `predicted` are each list's first such day, null when it was not cut, and
 * `first` is the earlier: from it on, a day may show less than it holds.
 */
export function upcomingCuts(upcoming: UpcomingResponse): {
  planned: string | null;
  predicted: string | null;
  first: string | null;
} {
  const cut = (rows: { day: string }[], truncated: boolean) =>
    truncated ? (rows.at(-1)?.day ?? upcoming.from) : null;
  const planned = cut(upcoming.planned, upcoming.planned_truncated);
  const predicted = cut(upcoming.predicted, upcoming.predicted_truncated);
  const first = [planned, predicted].filter((day) => day !== null).sort()[0] ?? null;
  return { planned, predicted, first };
}
