/**
 * Which stories the calendar asks for: the month on screen, the day opened
 * from it, and that a planned story reaches the month however busy the queue
 * is. The page, an async server component, is called directly with its two
 * doors mocked — the session guard and the target fetch — and the returned
 * element tree is read without rendering it (`environment: "node"`).
 *
 * The page makes several reads, so `readOf()` tells them apart by their path
 * and query, and `answer()` answers every read from one table.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactElement, ReactNode } from "react";

const { workspaceFetch } = vi.hoisted(() => ({ workspaceFetch: vi.fn() }));

vi.mock("@/lib/page-guards", () => ({
  requireWorkspacePage: async () => ({ workspaceId: "ws-1" }),
}));
vi.mock("@/lib/workspaces", () => ({ workspaceFetch }));

import CalendarPage from "./page";
import { CalendarDay } from "@/components/dashboard/media/calendar-day";
import { ContentCalendar } from "@/components/dashboard/media/content-calendar";
import { RouterUnavailable } from "@/components/workspace/router-unavailable";
import { QUEUE_STATES } from "@/lib/dashboard-payloads";
import { LIST_LIMIT_MAX, type Intent } from "@/lib/intents";

/** Depth-first walk of a returned tree, children flattened. */
function* walk(node: ReactNode): Generator<ReactElement> {
  if (node === null || node === undefined || typeof node !== "object") return;
  if (Array.isArray(node)) {
    for (const child of node) yield* walk(child);
    return;
  }
  const el = node as ReactElement<{ children?: ReactNode }>;
  yield el;
  yield* walk(el.props?.children);
}

const ok = (data: unknown) => ({ ok: true, data });
const rows = (...intents: Intent[]) => ok({ intents, limit: LIST_LIMIT_MAX });
const DOWN = { ok: false, status: 503, error: "http_503" };

/** A full row of the intents read; its file is named after its id. */
function story(id: string, over: Partial<Intent> = {}): Intent {
  return {
    id,
    state: "scheduled",
    ig_account_id: "a1",
    media_item_id: `m-${id}`,
    schedule_slot_at: "2026-10-20T14:00:00Z",
    approval_mode: "manual",
    published_via: null,
    publish_step: null,
    cancel_requested: false,
    ig_permalink: null,
    entered_state_at: "2026-10-07T12:00:00Z",
    created_at: "2026-10-01T12:00:00Z",
    origin: "cadence",
    scheduled_by_user_id: null,
    scheduled_by: null,
    tz: "UTC",
    miss_reason: null,
    file_name: `${id}.jpg`,
    media_kind: "image",
    has_thumbnail: false,
    thumbnail_version: "0123456789abcdef",
    caption: null,
    category: "memes",
    link_url: null,
    account_handle: null,
    account_display_name: null,
    ...over,
  };
}

type Read = "month" | "day" | "queue" | "planned" | "predicted" | "stats" | "config";

/** Which read a path is. */
function readOf(path: string): Read {
  const [head, query = ""] = path.split("?");
  if (head === "") return "config";
  if (head === "stats") return "stats";
  if (head === "intents/days") return "month";
  if (head !== "intents") throw new Error(`unexpected read: ${path}`);
  const q = new URLSearchParams(query);
  if (q.has("from")) return "day";
  if (q.get("origin") === "planned") return "planned";
  if (q.get("state") === "scheduled") return "predicted";
  return "queue";
}

/** Answer each read from one table; `over` replaces some of it. */
function answer(over: Partial<Record<Read, unknown>> = {}) {
  const table: Record<Read, unknown> = {
    month: ok({ days: [], per_day: 3 }),
    day: rows(),
    queue: rows(),
    planned: rows(),
    predicted: rows(),
    stats: ok({ intents_by_state: {}, posts_by_day: [] }),
    config: ok({
      tz: "America/New_York",
      posts_per_day: null,
      posting_hours_start: null,
      posting_hours_end: null,
    }),
    ...over,
  };
  workspaceFetch.mockImplementation(async (path: string) => table[readOf(path)]);
}

/** The page for these `?month=` and `?day=`. */
async function page(params: { month?: string; day?: string } = {}) {
  return [...walk(await CalendarPage({ searchParams: Promise.resolve(params) }))];
}

/** The props the page hands one component. */
function propsOf<P>(elements: ReactElement[], type: unknown): P {
  const el = elements.find((e) => e.type === type);
  expect(el, "the page does not render it").toBeDefined();
  return el!.props as P;
}

/** The query of the page's one read of this kind. */
function query(read: Read): URLSearchParams {
  const paths = workspaceFetch.mock.calls
    .map(([path]) => path as string)
    .filter((path) => readOf(path) === read);
  expect(paths, `the page made ${paths.length} ${read} reads`).toHaveLength(1);
  return new URLSearchParams(paths[0].split("?")[1]);
}

type CalendarProps = Parameters<typeof ContentCalendar>[0];
type DayProps = Parameters<typeof CalendarDay>[0];

beforeEach(() => {
  workspaceFetch.mockReset();
  // 9:30 PM on Thursday, Oct 1 in New York: already Oct 2 in UTC.
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-10-02T01:30:00Z"));
});

afterEach(() => {
  vi.useRealTimers();
});

describe("the calendar's month (#1634)", () => {
  it("draws this month in the workspace's zone, its whole grid counted", async () => {
    answer();
    const calendar = propsOf<CalendarProps>(await page(), ContentCalendar);
    expect(calendar.month).toEqual({ year: 2026, month: 10 });
    expect(calendar.navigable).toBe(true);
    const q = query("month");
    // Posted only: a skipped or rejected story drawn under Posted reads as posted.
    expect(q.get("state")).toBe("posted");
    // October 2026 in Monday-first weeks: Sep 28 to Nov 1, so up to Nov 2.
    expect([q.get("from"), q.get("to")]).toEqual(["2026-09-28", "2026-11-02"]);
    expect(q.get("per_day")).toBe("3");
  });

  it("draws the month it is asked for, and hands it the month read's days", async () => {
    const days = [{ date: "2026-09-03", count: 15, newest: [] }];
    answer({ month: ok({ days, per_day: 3 }) });
    const calendar = propsOf<CalendarProps>(await page({ month: "2026-09" }), ContentCalendar);
    expect(calendar.month).toEqual({ year: 2026, month: 9 });
    expect(calendar.history).toEqual(days);
    expect([query("month").get("from"), query("month").get("to")]).toEqual([
      "2026-08-31",
      "2026-10-05",
    ]);
  });

  it("reads a month it cannot parse as this one", async () => {
    answer();
    const calendar = propsOf<CalendarProps>(await page({ month: "2026-13" }), ContentCalendar);
    expect(calendar.month).toEqual({ year: 2026, month: 10 });
  });

  it("renders the unavailable state, not a calendar, when the month read fails", async () => {
    answer({ month: DOWN });
    const elements = await page();
    expect(elements.some((el) => el.type === RouterUnavailable)).toBe(true);
    expect(elements.some((el) => el.type === ContentCalendar)).toBe(false);
  });
});

describe("a day opened from the month (#1634)", () => {
  it("lists the day's stories in every state, in time order", async () => {
    const day = [story("a", { state: "posted" }), story("b", { state: "skipped" })];
    answer({ day: rows(...day) });
    const elements = await page({ month: "2026-10", day: "2026-10-03" });
    const q = query("day");
    expect([q.get("from"), q.get("to")]).toEqual(["2026-10-03", "2026-10-04"]);
    expect(q.get("state")).toBeNull();
    expect(q.get("order")).toBe("asc");
    expect(q.get("limit")).toBe(String(LIST_LIMIT_MAX));
    const opened = propsOf<DayProps>(elements, CalendarDay);
    expect(opened.date).toBe("2026-10-03");
    expect(opened.intents).toEqual(day);
    expect(opened.closeHref).toBe("?month=2026-10");
    expect(opened.truncatedAt).toBeNull();
    expect(propsOf<CalendarProps>(elements, ContentCalendar).selected).toBe("2026-10-03");
  });

  it("opens the day's own month when no month is given", async () => {
    answer();
    const elements = await page({ day: "2026-11-15" });
    expect(propsOf<CalendarProps>(elements, ContentCalendar).month).toEqual({
      year: 2026,
      month: 11,
    });
    expect(propsOf<DayProps>(elements, CalendarDay).date).toBe("2026-11-15");
  });

  it("opens nothing for a day off the month's grid", async () => {
    answer();
    const elements = await page({ month: "2026-10", day: "2026-12-03" });
    expect(workspaceFetch.mock.calls.map(([p]) => readOf(p as string))).not.toContain("day");
    expect(elements.some((el) => el.type === CalendarDay)).toBe(false);
  });

  it("says when the day reached the read's limit", async () => {
    answer({
      day: ok({ intents: Array.from({ length: 2 }, (_, n) => story(`s${n}`)), limit: 2 }),
    });
    const elements = await page({ month: "2026-10", day: "2026-10-03" });
    expect(propsOf<DayProps>(elements, CalendarDay).truncatedAt).toBe(2);
  });

  it("renders the unavailable state when the day read fails", async () => {
    answer({ day: DOWN });
    const elements = await page({ month: "2026-10", day: "2026-10-03" });
    expect(elements.some((el) => el.type === RouterUnavailable)).toBe(true);
  });
});

describe("the calendar's upcoming reads", () => {
  it("splits the upcoming stories by origin, so each is in one read", async () => {
    answer();
    await page();
    const queue = query("queue");
    expect(queue.get("origin")).toBe("cadence");
    expect(queue.get("state")).toBe(QUEUE_STATES);
    const planned = query("planned");
    expect(planned.get("state")).toBe(QUEUE_STATES);
    expect(planned.get("limit")).toBe(String(LIST_LIMIT_MAX));
  });

  it("draws every planned story, however many are queued ahead of it", async () => {
    const soonest = Array.from({ length: 10 }, (_, n) =>
      story(`q${n}`, { schedule_slot_at: "2026-10-08T14:00:00Z" }),
    );
    answer({
      queue: rows(...soonest),
      planned: rows(
        story("later", { origin: "planned", schedule_slot_at: "2026-10-24T14:00:00Z" }),
        story("waiting", { origin: "planned", state: "awaiting_approval" }),
      ),
    });
    const { queue } = propsOf<CalendarProps>(await page(), ContentCalendar);
    expect(queue).toHaveLength(12);
    expect(queue.filter((item) => item.planned).map((item) => item.media_name)).toEqual([
      "later.jpg",
      "waiting.jpg",
    ]);
  });

  it("renders the unavailable state, not a calendar, when the planned read fails", async () => {
    answer({ planned: DOWN });
    const elements = await page();
    expect(elements.some((el) => el.type === RouterUnavailable)).toBe(true);
    expect(elements.some((el) => el.type === ContentCalendar)).toBe(false);
  });
});
