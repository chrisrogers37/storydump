/**
 * Where the overview puts the runway card (#1478), and that a failed runway
 * read renders the unavailable state instead of a page missing the figure.
 * The page is called directly with its two doors mocked and the returned
 * element tree is read without rendering it (`environment: "node"`), as
 * `overview-conditions.test.tsx` does.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactElement, ReactNode } from "react";

const { workspaceFetch } = vi.hoisted(() => ({ workspaceFetch: vi.fn() }));

vi.mock("@/lib/page-guards", () => ({
  requireWorkspacePage: async () => ({ workspaceId: "ws-1" }),
}));
vi.mock("@/lib/workspaces", () => ({ workspaceFetch }));

import DashboardPage from "./page";
import { AnalyticsCards } from "@/components/dashboard/analytics-cards";
import { RunwayCard } from "@/components/dashboard/runway-card";
import { RouterUnavailable } from "@/components/workspace/router-unavailable";
import type { RunwayRow } from "@/lib/runway";

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

const RUNWAY = {
  below_days: 7,
  accounts: [
    {
      id: "a1",
      handle: "storyco",
      display_name: null,
      state: "active",
      posting: true,
      posts_per_day: 3,
      eligible: 20,
      // Whole days of 20 files at 3 a day, counted down on the server.
      days_left: 6,
      low: true,
    },
  ],
};

/** Every read the page makes, answered by the first segment of its path. */
function answer(runway: unknown) {
  const table: Record<string, unknown> = {
    stats: ok({
      intents_by_state: {},
      media_by_state: {},
      media_never_posted: 0,
      media_by_category: {},
      posted_by_category: {},
      posts_by_day: [],
      accounts: 1,
      sources: 1,
    }),
    runway,
    intents: ok({ intents: [], limit: 10 }),
    accounts: ok({ accounts: [] }),
    sources: ok({ sources: [] }),
  };
  workspaceFetch.mockImplementation(async (path: string) => {
    const read = path.split(/[?/]/)[0];
    if (!(read in table)) throw new Error(`unexpected read: ${path}`);
    return table[read];
  });
}

beforeEach(() => {
  workspaceFetch.mockReset();
});

describe("the overview's runway card", () => {
  it("renders each account's days left, below the analytics", async () => {
    answer(ok(RUNWAY));
    const elements = [...walk(await DashboardPage())];

    const card = elements.find((el) => el.type === RunwayCard);
    expect(card, "the overview renders no runway card").toBeDefined();
    const { rows, belowDays } = card!.props as { rows: RunwayRow[]; belowDays: number };
    expect(belowDays).toBe(7);
    expect(rows).toEqual([
      {
        key: "a1",
        name: "storyco",
        headline: "About 6 days",
        detail: "20 files at 3 a day",
        low: true,
      },
    ]);

    const cards = elements.findIndex((el) => el.type === AnalyticsCards);
    expect(cards).toBeGreaterThan(-1);
    expect(elements.indexOf(card!)).toBeGreaterThan(cards);
  });

  it("reads the runway once, by its own path", async () => {
    answer(ok(RUNWAY));
    await DashboardPage();
    const paths = workspaceFetch.mock.calls.map(([path]) => path);
    expect(paths.filter((path) => path === "runway")).toHaveLength(1);
  });

  it("a failed runway read is the unavailable state, not a page without it", async () => {
    answer({ ok: false, status: 503, error: "http_503" });
    const elements = [...walk(await DashboardPage())];
    expect(elements.some((el) => el.type === RouterUnavailable)).toBe(true);
    expect(elements.some((el) => el.type === RunwayCard)).toBe(false);
  });
});
