/**
 * Where the overview puts its condition panel, and that a failed read renders
 * the unavailable state instead of it (the rule is stated on
 * `ConditionsPanel`). The page, an async server component, is called directly
 * with its two doors mocked — the session guard and the target fetch — and the
 * returned element tree is read without rendering it (`environment: "node"`).
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
import { ConditionsPanel } from "@/components/dashboard/conditions-panel";
import { PostingMixCard } from "@/components/dashboard/posting-mix-card";
import { RouterUnavailable } from "@/components/workspace/router-unavailable";
import type { Condition, SetupStep } from "@/lib/conditions";
import type { FolderMix } from "@/lib/dashboard-payloads";

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

type Read = "stats" | "intents" | "accounts" | "sources" | "category-mix";

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
    ...overrides,
  };
  workspaceFetch.mockImplementation(async (path: string) => {
    const read = path.split(/[?/]/)[0] as Read;
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

  it.each<Read>(["accounts", "sources", "stats", "category-mix"])(
    "a failed %s read is the unavailable state — never an all-clear",
    async (read) => {
      answer({ [read]: DOWN });
      const elements = [...walk(await DashboardPage())];
      expect(elements.some((el) => el.type === RouterUnavailable)).toBe(true);
      expect(elements.some((el) => el.type === ConditionsPanel)).toBe(false);
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
