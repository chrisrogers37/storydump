/**
 * Which stories the calendar asks for: the month on screen, what is coming on
 * it, the day opened from it, and that a planned story reaches the month
 * however busy the queue is. The page, an async server component, is called
 * directly with its two doors mocked — the session guard and the target fetch
 * — and the returned element tree is read without rendering it
 * (`environment: "node"`).
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
import type { PredictedSlot, UpcomingResponse } from "@/lib/calendar-month";
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

/** A slot the cadence will open, on its day in the workspace's zone. */
function slot(day: string, hour: number, over: Partial<PredictedSlot> = {}): PredictedSlot {
  return {
    kind: "predicted",
    schedule_slot_at: `${day}T${String(hour).padStart(2, "0")}:00:00+00:00`,
    day,
    tz: "America/New_York",
    ig_account_id: "a1",
    account_handle: "example.brand",
    account_display_name: "Example Co",
    ...over,
  };
}

/** The upcoming read's answer for October's grid; `over` replaces some of it. */
const coming = (over: Partial<UpcomingResponse> = {}) =>
  ok({
    from: "2026-09-28",
    to: "2026-11-02",
    planned: [],
    planned_truncated: false,
    predicted: [],
    predicted_truncated: false,
    ...over,
  });

type Read = "month" | "day" | "queue" | "planned" | "upcoming" | "stats" | "config";

/** Which read a path is. */
function readOf(path: string): Read {
  const [head, query = ""] = path.split("?");
  if (head === "") return "config";
  if (head === "stats") return "stats";
  if (head === "intents/days") return "month";
  if (head === "upcoming") return "upcoming";
  if (head !== "intents") throw new Error(`unexpected read: ${path}`);
  const q = new URLSearchParams(query);
  if (q.has("from")) return "day";
  if (q.get("origin") === "planned") return "planned";
  if (q.get("origin") === "cadence") return "queue";
  throw new Error(`unexpected intents read: ${path}`);
}

/** Answer each read from one table; `over` replaces some of it. */
function answer(over: Partial<Record<Read, unknown>> = {}) {
  const table: Record<Read, unknown> = {
    month: ok({ days: [], per_day: 3 }),
    day: rows(),
    queue: rows(),
    planned: rows(),
    upcoming: coming(),
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
  it("splits the stories under way by origin, and leaves a planned one still scheduled to the upcoming read", async () => {
    answer();
    await page();
    const queue = query("queue");
    expect(queue.get("origin")).toBe("cadence");
    expect(queue.get("state")).toBe(QUEUE_STATES);
    const planned = query("planned");
    // Every queue state but `scheduled`: a planned story still scheduled is in
    // the upcoming read, so each planned story is in one read.
    expect(planned.get("state")!.split(",")).toEqual(
      QUEUE_STATES.split(",").filter((state) => state !== "scheduled"),
    );
    expect(planned.get("limit")).toBe(String(LIST_LIMIT_MAX));
  });

  it("draws every planned story, however many are queued ahead of it", async () => {
    const soonest = Array.from({ length: 10 }, (_, n) =>
      story(`q${n}`, { schedule_slot_at: "2026-10-08T14:00:00Z" }),
    );
    answer({
      queue: rows(...soonest),
      planned: rows(story("waiting", { origin: "planned", state: "awaiting_approval" })),
      upcoming: coming({
        planned: [
          {
            ...story("later", { origin: "planned", schedule_slot_at: "2026-10-24T14:00:00Z" }),
            day: "2026-10-24",
          },
        ],
      }),
    });
    const { queue } = propsOf<CalendarProps>(await page(), ContentCalendar);
    expect(queue).toHaveLength(12);
    expect(
      queue.filter((item) => item.planned).map((item) => [item.media_name, item.day]),
    ).toEqual([
      ["waiting.jpg", undefined],
      ["later.jpg", "2026-10-24"],
    ]);
  });

  it("hands the month and the day view the workspace, and each queued story its picture (#1634 Phase 4)", async () => {
    const pictured = story("pictured", { has_thumbnail: true, thumbnail_version: "v9" });
    answer({ queue: rows(pictured) });
    const elements = await page({ month: "2026-10", day: "2026-10-03" });
    const calendar = propsOf<CalendarProps>(elements, ContentCalendar);
    expect(calendar.workspaceId).toBe("ws-1");
    expect(calendar.queue[0].media).toMatchObject({
      file_name: "pictured.jpg",
      media_item_id: pictured.media_item_id,
      has_thumbnail: true,
      thumbnail_version: "v9",
    });
    expect(propsOf<DayProps>(elements, CalendarDay).workspaceId).toBe("ws-1");
  });

  it("renders the unavailable state, not a calendar, when the planned read fails", async () => {
    answer({ planned: DOWN });
    const elements = await page();
    expect(elements.some((el) => el.type === RouterUnavailable)).toBe(true);
    expect(elements.some((el) => el.type === ContentCalendar)).toBe(false);
  });

  it("hands the month a story's picture fields, not its whole row", async () => {
    answer({ queue: rows(story("pictured", { caption: "not the chip's" })) });
    const { queue } = propsOf<CalendarProps>(await page(), ContentCalendar);
    expect(Object.keys(queue[0].media!).sort()).toEqual([
      "file_name",
      "has_thumbnail",
      "media_item_id",
      "media_kind",
      "thumbnail_version",
    ]);
  });
});

describe("what is coming on the month (#1634 Phase 3b)", () => {
  it("asks what is coming on the whole grid, past days and future alike", async () => {
    answer();
    await page({ month: "2026-12" });
    // December 2026 in Monday-first weeks: Nov 30 to Jan 3, so up to Jan 4.
    expect([query("upcoming").get("from"), query("upcoming").get("to")]).toEqual([
      "2026-11-30",
      "2027-01-04",
    ]);
  });

  it("hands the month a count of predicted slots per day, never the slots", async () => {
    answer({
      upcoming: coming({
        predicted: [slot("2026-10-20", 13), slot("2026-10-20", 17), slot("2026-10-21", 13)],
      }),
    });
    const calendar = propsOf<CalendarProps>(await page(), ContentCalendar);
    expect(calendar.predicted).toEqual([
      { date: "2026-10-20", count: 2 },
      { date: "2026-10-21", count: 1 },
    ]);
    expect(calendar.incompleteFrom).toBeNull();
  });

  it("marks the month from the first day a cut list may be short on, whichever list it is", async () => {
    const planned = (day: string) => [{ ...story("p", { origin: "planned" }), day }];
    const predicted = [slot("2026-10-20", 13), slot("2026-10-22", 13)];

    answer({
      upcoming: coming({
        planned: planned("2026-10-25"),
        planned_truncated: true,
        predicted,
        predicted_truncated: true,
      }),
    });
    expect(propsOf<CalendarProps>(await page(), ContentCalendar).incompleteFrom).toBe("2026-10-22");

    // The planned cut first: the month is marked from it, not from the predicted one.
    answer({
      upcoming: coming({
        planned: planned("2026-10-21"),
        planned_truncated: true,
        predicted,
        predicted_truncated: true,
      }),
    });
    expect(propsOf<CalendarProps>(await page(), ContentCalendar).incompleteFrom).toBe("2026-10-21");
  });

  it("lists a day's predicted slots in its day view, and says when they were cut", async () => {
    const predicted = [slot("2026-10-20", 13), slot("2026-10-20", 17), slot("2026-10-21", 13)];
    answer({ upcoming: coming({ predicted }) });
    const opened = propsOf<DayProps>(await page({ month: "2026-10", day: "2026-10-20" }), CalendarDay);
    expect(opened.predicted).toEqual(predicted.slice(0, 2));
    expect(opened.predictedCut).toBe(false);

    answer({ upcoming: coming({ predicted, predicted_truncated: true }) });
    const before = propsOf<DayProps>(await page({ month: "2026-10", day: "2026-10-20" }), CalendarDay);
    expect(before.predictedCut).toBe(false);
    const after = propsOf<DayProps>(await page({ month: "2026-10", day: "2026-10-21" }), CalendarDay);
    expect(after.predictedCut).toBe(true);
  });

  it("keeps an opened day's predicted slots whole when only the planned list was cut", async () => {
    const predicted = [slot("2026-10-22", 13)];
    answer({
      upcoming: coming({
        planned: [{ ...story("p", { origin: "planned" }), day: "2026-10-20" }],
        planned_truncated: true,
        predicted,
      }),
    });
    const elements = await page({ month: "2026-10", day: "2026-10-22" });
    // The month may hold more planned stories from the 20th; the day's slots are all here.
    expect(propsOf<CalendarProps>(elements, ContentCalendar).incompleteFrom).toBe("2026-10-20");
    const opened = propsOf<DayProps>(elements, CalendarDay);
    expect(opened.predicted).toEqual(predicted);
    expect(opened.predictedCut).toBe(false);
  });

  it("renders the unavailable state, not a calendar, when the upcoming read fails", async () => {
    answer({ upcoming: DOWN });
    const elements = await page();
    expect(elements.some((el) => el.type === RouterUnavailable)).toBe(true);
    expect(elements.some((el) => el.type === ContentCalendar)).toBe(false);
  });
});
