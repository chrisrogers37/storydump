/**
 * Where the overview puts its condition panel, and that a failed read renders
 * the unavailable state instead of it (the rule is stated on
 * `ConditionsPanel`). The page, an async server component, is called directly
 * with its two doors mocked — the session guard and the target fetch — and the
 * returned element tree is read without rendering it (`environment: "node"`).
 *
 * Every test of the overview page uses this one harness: `answer()` is the
 * single table of the reads the page makes, so a read the page adds is answered
 * in one place rather than in copies that drift apart.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactElement, ReactNode } from "react";

const { workspaceFetch, viewer } = vi.hoisted(() => ({
  workspaceFetch: vi.fn(),
  viewer: { role: "admin" },
}));

vi.mock("@/lib/page-guards", () => ({
  requireWorkspacePage: async () => ({
    workspaceId: "ws-1",
    session: { workspaces: [{ id: "ws-1", name: "WS", role: viewer.role, state: "active" }] },
  }),
}));
vi.mock("@/lib/workspaces", () => ({ workspaceFetch }));

import DashboardPage from "./page";
import { AnalyticsCards } from "@/components/dashboard/analytics-cards";
import { ConditionsPanel } from "@/components/dashboard/conditions-panel";
import { PostingMixCard } from "@/components/dashboard/posting-mix-card";
import { RecentActivity } from "@/components/dashboard/recent-activity";
import { RunwayCard } from "@/components/dashboard/runway-card";
import { RouterUnavailable } from "@/components/workspace/router-unavailable";
import type { Condition, SetupStep } from "@/lib/conditions";
import type { FolderMix } from "@/lib/dashboard-payloads";
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
const DOWN = { ok: false, status: 503, error: "http_503" };

/**
 * Every read the page makes, by the first segment of its path (the
 * workspace's own config is its empty path). `answer()`'s table must answer
 * each (a `Record` over them) and the failed-read cases run over all of them,
 * so a read listed here is both answered and failed.
 */
const READS = [
  "stats",
  "intents",
  "accounts",
  "sources",
  "category-mix",
  "runway",
  "config",
] as const;
type Read = (typeof READS)[number];

const STATS = {
  intents_by_state: { review_required: 2, scheduled: 3 },
  media_by_state: {},
  media_never_posted: 0,
  media_by_category: {},
  posted_by_source: {},
  posts_by_day: [],
  accounts: 1,
  sources: 1,
};

const RUNWAY = {
  // Not the server's default, so a level hardcoded in the page cannot pass.
  below_days: 5,
  accounts: [
    {
      id: "a1",
      handle: "storyco",
      display_name: null,
      state: "active",
      posting: true,
      posts_per_day: 3,
      eligible: 14,
      // Whole days of 14 files at 3 a day, counted down on the server.
      days_left: 4,
      low: true,
    },
  ],
};

/** Answer each read by the first segment of its path; `overrides` replaces one. */
function answer(overrides: Partial<Record<Read, unknown>> = {}) {
  const table: Record<Read, unknown> = {
    stats: ok(STATS),
    "category-mix": ok({ rows: [] }),
    intents: ok({ intents: [], limit: 10 }),
    accounts: ok({
      accounts: [
        {
          id: "a1",
          provider_account_ref: "manual:storyco",
          handle: "storyco",
          display_name: null,
          state: "reauth_required",
          next_slot_at: null,
          last_posted_at: null,
          credential_status: "active",
          credential_connected_at: null,
        },
      ],
    }),
    sources: ok({ sources: [] }),
    runway: ok({ below_days: 5, accounts: [] }),
    config: ok({ tz: "America/New_York" }),
    ...overrides,
  };
  workspaceFetch.mockImplementation(async (path: string) => {
    // The workspace's own config is its empty path.
    const read = (path.split(/[?/]/)[0] || "config") as Read;
    if (!(read in table)) throw new Error(`unexpected read: ${path}`);
    return table[read];
  });
}

beforeEach(() => {
  workspaceFetch.mockReset();
});

describe("the overview's condition panel", () => {
  it("renders first, above the analytics, from what the reads describe", async () => {
    answer();
    const tree = await DashboardPage();
    const elements = [...walk(tree)];

    const panel = elements.find((el) => el.type === ConditionsPanel);
    expect(panel, "the overview renders no condition panel").toBeDefined();
    const { conditions } = panel!.props as { conditions: Condition[] };
    expect(conditions.map((c) => c.key)).toEqual(["account:a1", "review_required"]);

    const cards = elements.findIndex((el) => el.type === AnalyticsCards);
    expect(cards).toBeGreaterThan(-1);
    expect(elements.indexOf(panel!)).toBeLessThan(cards);
  });

  it("reads the accounts and sources lists for it", async () => {
    answer();
    await DashboardPage();
    const paths = workspaceFetch.mock.calls.map(([path]) => path);
    expect(paths).toContain("accounts");
    expect(paths).toContain("sources");
  });

  it("hands the panel the first missing setup step from the same reads", async () => {
    answer({ accounts: ok({ accounts: [] }), stats: ok({ ...STATS, intents_by_state: {} }) });
    const panel = [...walk(await DashboardPage())].find(
      (el) => el.type === ConditionsPanel,
    );
    const { conditions, setupStep } = panel!.props as {
      conditions: Condition[];
      setupStep: SetupStep | null;
    };
    expect(conditions).toEqual([]);
    expect(setupStep?.number).toBe(1);
    expect(setupStep?.href).toBeDefined();
  });

  it("a member's setup step waits on an admin, with no button", async () => {
    viewer.role = "member";
    try {
      answer({ accounts: ok({ accounts: [] }), stats: ok({ ...STATS, intents_by_state: {} }) });
      const panel = [...walk(await DashboardPage())].find(
        (el) => el.type === ConditionsPanel,
      );
      const { setupStep } = panel!.props as { setupStep: SetupStep | null };
      expect(setupStep?.title).toBe("Waiting on an admin to connect Instagram");
      expect(setupStep?.href).toBeUndefined();
    } finally {
      viewer.role = "admin";
    }
  });

  it("a set-up workspace hands it no setup step", async () => {
    answer({
      sources: ok({
        sources: [{ id: "s1", provider: "gdrive", state: "active", folder_ref: "f1", folder_name: "memes", removed: false }],
      }),
    });
    const panel = [...walk(await DashboardPage())].find(
      (el) => el.type === ConditionsPanel,
    );
    expect((panel!.props as { setupStep: SetupStep | null }).setupStep).toBeNull();
  });

  it.each(READS)(
    "a failed %s read is the unavailable state — never an all-clear",
    async (read) => {
      answer({ [read]: DOWN });
      const elements = [...walk(await DashboardPage())];
      expect(elements.some((el) => el.type === RouterUnavailable)).toBe(true);
      expect(elements.some((el) => el.type === ConditionsPanel)).toBe(false);
      expect(elements.some((el) => el.type === RunwayCard)).toBe(false);
    },
  );
});

describe("the overview's mix card", () => {
  it("plans from the category-mix read and shares from stats", async () => {
    answer({
      stats: ok({ ...STATS, posted_by_source: { s1: 3, s2: 1 } }),
      "category-mix": ok({
        rows: [
          { source_id: "s1", provider: "gdrive", name: "memes", state: "active", media_count: 8, ratio: 0.5, effective: 50 },
          { source_id: "s2", provider: "gdrive", name: "merch", state: "active", media_count: 2, ratio: 0.5, effective: 50 },
        ],
      }),
    });
    const card = [...walk(await DashboardPage())].find(
      (el) => el.type === PostingMixCard,
    );
    expect(card, "the overview renders no mix card").toBeDefined();
    const { mix } = card!.props as { mix: FolderMix };
    expect(mix.folders.map((f) => [f.name, f.planned, f.posted])).toEqual([
      ["memes", 50, 75],
      ["merch", 50, 25],
    ]);
    expect(workspaceFetch.mock.calls.map(([path]) => path)).toContain(
      "category-mix",
    );
  });
});

describe("the overview's recent activity", () => {
  const zoneOf = async () => {
    const list = [...walk(await DashboardPage())].find((el) => el.type === RecentActivity);
    expect(list, "the overview renders no recent activity").toBeDefined();
    return (list!.props as { tz: string }).tz;
  };

  it("reads its times in the workspace's zone (#1511)", async () => {
    answer();
    expect(await zoneOf()).toBe("America/New_York");
  });

  it("reads them in UTC for a workspace with no zone set", async () => {
    answer({ config: ok({ tz: null }) });
    expect(await zoneOf()).toBe("UTC");
  });
});

describe("the overview's runway card", () => {
  it("renders each account's days left, below the analytics", async () => {
    answer({ runway: ok(RUNWAY) });
    const elements = [...walk(await DashboardPage())];

    const card = elements.find((el) => el.type === RunwayCard);
    expect(card, "the overview renders no runway card").toBeDefined();
    const { rows, belowDays } = card!.props as {
      rows: RunwayRow[];
      belowDays: number;
    };
    expect(belowDays).toBe(5);
    expect(rows).toEqual([
      {
        key: "a1",
        name: "storyco",
        headline: "About 4 days",
        detail: "14 files at 3 a day",
        low: true,
      },
    ]);

    const cards = elements.findIndex((el) => el.type === AnalyticsCards);
    expect(cards).toBeGreaterThan(-1);
    expect(elements.indexOf(card!)).toBeGreaterThan(cards);
  });

  it("reads the runway once, by its own path", async () => {
    answer({ runway: ok(RUNWAY) });
    await DashboardPage();
    const paths = workspaceFetch.mock.calls.map(([path]) => path);
    expect(paths.filter((path) => path === "runway")).toHaveLength(1);
  });
});
