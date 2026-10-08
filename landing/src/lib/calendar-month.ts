/**
 * The calendar's month and day, as local dates in the workspace's zone (#1634).
 *
 * Every date here is a calendar date, `YYYY-MM-DD`, with no zone attached: the
 * API turns `?from=` and `?to=` into the instants those days start in the
 * workspace's own zone, so the month on screen and the month the API counts
 * are the same days. The arithmetic is on UTC dates, which have no zone to
 * disagree about.
 */

/** A month, `month` 1-12. */
export type Month = { year: number; month: number };

/** One local day of `GET /workspaces/{ws}/intents/days`. */
export type IntentDay = {
  date: string;
  /** Every intent the day holds in the asked states: a COUNT, not a list's length. */
  count: number;
  /** The day's newest, at most the read's `per_day`. */
  newest: {
    id: string;
    state: string;
    schedule_slot_at: string;
    file_name: string;
    category: string | null;
  }[];
};

export type IntentDaysResponse = { days: IntentDay[]; per_day: number };

/** Names each day of the month carries: the chips a calendar cell draws. */
export const NAMES_PER_DAY = 3;

const MONTH = /^(\d{4})-(\d{2})$/;
const DAY = /^\d{4}-\d{2}-\d{2}$/;

const isoDay = (d: Date) => d.toISOString().slice(0, 10);

/** `?month=2026-10` → the month, or null when it is not one. */
export function parseMonth(value: unknown): Month | null {
  const found = typeof value === "string" ? MONTH.exec(value) : null;
  if (!found) return null;
  const month = Number(found[2]);
  return month >= 1 && month <= 12 ? { year: Number(found[1]), month } : null;
}

/** `?day=2026-10-03` → the date, or null when it is not a real one. */
export function parseDay(value: unknown): string | null {
  if (typeof value !== "string" || !DAY.test(value)) return null;
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
