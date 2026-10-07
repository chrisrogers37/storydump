/**
 * Which stories the calendar asks for, and that a planned story reaches the
 * month however busy the queue is. The page, an async server component, is
 * called directly with its two doors mocked — the session guard and the
 * target fetch — and the returned element tree is read without rendering it
 * (`environment: "node"`).
 *
 * The calendar makes four `intents` reads, so `readOf()` tells them apart by
 * their query, and `answer()` answers every read from one table.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactElement, ReactNode } from "react";

const { workspaceFetch } = vi.hoisted(() => ({ workspaceFetch: vi.fn() }));

vi.mock("@/lib/page-guards", () => ({
  requireWorkspacePage: async () => ({ workspaceId: "ws-1" }),
}));
vi.mock("@/lib/workspaces", () => ({ workspaceFetch }));

import CalendarPage from "./page";
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
const rows = (...intents: Intent[]) => ok({ intents });
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
    thumbnail_url: null,
    caption: null,
    category: "memes",
    account_handle: null,
    account_display_name: null,
    ...over,
  };
}

type Read = "history" | "queue" | "planned" | "predicted" | "stats" | "config";

/**
 * Which read a path is. History is the one asking for a posted state, so a
 * read that slipped back to all three outcomes still lands here, and fails.
 */
function readOf(path: string): Read {
  const [head, query = ""] = path.split("?");
  if (head === "") return "config";
  if (head === "stats") return "stats";
  if (head !== "intents") throw new Error(`unexpected read: ${path}`);
  const q = new URLSearchParams(query);
  if ((q.get("state") ?? "").split(",").includes("posted")) return "history";
  if (q.get("origin") === "planned") return "planned";
  if (q.get("state") === "scheduled") return "predicted";
  return "queue";
}

/** Answer each read from one table; `over` replaces some of it. */
function answer(over: Partial<Record<Read, unknown>> = {}) {
  const table: Record<Read, unknown> = {
    history: rows(),
    queue: rows(),
    planned: rows(),
    predicted: rows(),
    stats: ok({ intents_by_state: {}, posts_by_day: [] }),
    config: ok({
      tz: "UTC",
      posts_per_day: null,
      posting_hours_start: null,
      posting_hours_end: null,
    }),
    ...over,
  };
  workspaceFetch.mockImplementation(async (path: string) => table[readOf(path)]);
}

/** The lanes the page hands the calendar. */
async function lanes() {
  const el = [...walk(await CalendarPage())].find((e) => e.type === ContentCalendar);
  expect(el, "the page renders no calendar").toBeDefined();
  return el!.props as Parameters<typeof ContentCalendar>[0];
}

/** The query of the page's one read of this kind. */
function query(read: Read): URLSearchParams {
  const paths = workspaceFetch.mock.calls
    .map(([path]) => path as string)
    .filter((path) => readOf(path) === read);
  expect(paths, `the page made ${paths.length} ${read} reads`).toHaveLength(1);
  return new URLSearchParams(paths[0].split("?")[1]);
}

beforeEach(() => {
  workspaceFetch.mockReset();
});

describe("the calendar's reads", () => {
  it("asks for the newest posted stories, up to the API's ceiling", async () => {
    answer();
    await lanes();
    const q = query("history");
    // The API sorts soonest first unless asked; oldest-first fills the lane
    // with outcomes from before the month on screen.
    expect(q.get("order")).toBe("desc");
    // Posted only: a skipped or rejected story drawn here reads as posted.
    expect(q.get("state")).toBe("posted");
    expect(q.get("limit")).toBe(String(LIST_LIMIT_MAX));
  });

  it("splits the upcoming stories by origin, so each is in one read", async () => {
    answer();
    await lanes();
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
    const { queue } = await lanes();
    expect(queue).toHaveLength(12);
    expect(queue.filter((item) => item.planned).map((item) => item.media_name)).toEqual([
      "later.jpg",
      "waiting.jpg",
    ]);
  });

  it("renders the unavailable state, not a calendar, when the planned read fails", async () => {
    answer({ planned: DOWN });
    const elements = [...walk(await CalendarPage())];
    expect(elements.some((el) => el.type === RouterUnavailable)).toBe(true);
    expect(elements.some((el) => el.type === ContentCalendar)).toBe(false);
  });
});
