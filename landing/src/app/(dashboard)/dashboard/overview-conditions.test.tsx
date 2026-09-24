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
import { RouterUnavailable } from "@/components/workspace/router-unavailable";
import type { Condition } from "@/lib/conditions";

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

type Read = "stats" | "intents" | "accounts" | "sources";

/** Answer each read by the first segment of its path; `overrides` replaces one. */
function answer(overrides: Partial<Record<Read, unknown>> = {}) {
  const table: Record<Read, unknown> = {
    stats: ok({
      intents_by_state: { review_required: 2, scheduled: 3 },
      media_by_state: {},
      media_never_posted: 0,
      media_by_category: {},
      posted_by_category: {},
      posts_by_day: [],
      accounts: 1,
      sources: 1,
    }),
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

  it.each<Read>(["accounts", "sources", "stats"])(
    "a failed %s read is the unavailable state — never an all-clear",
    async (read) => {
      answer({ [read]: DOWN });
      const elements = [...walk(await DashboardPage())];
      expect(elements.some((el) => el.type === RouterUnavailable)).toBe(true);
      expect(elements.some((el) => el.type === ConditionsPanel)).toBe(false);
    },
  );
});
