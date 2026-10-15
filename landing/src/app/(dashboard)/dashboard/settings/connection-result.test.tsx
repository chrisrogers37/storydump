/**
 * The applied-result banner on Settings (#1644). Where past posts are
 * a choice, the Instagram callback lands on `?connection=<id>`; the page reads
 * the result the id names and says its outcome. What is pinned: each outcome's
 * sentence, the line beside a result the mix has moved past, the link to the
 * current source status, that only an id-shaped query is read, that a result
 * the API will not give (404: expired, another session) is no banner, and that
 * the no-capability path's `?connected=instagram` banner is unchanged.
 *
 * As in `mix-card-choice.test.tsx`, the page, an async server component, is
 * called with its doors mocked and the returned tree is read without
 * rendering; the banner itself is rendered to markup.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { createElement, isValidElement, type ReactElement, type ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";

const { workspaceFetch } = vi.hoisted(() => ({ workspaceFetch: vi.fn() }));

vi.mock("@/lib/page-guards", () => ({
  requireWorkspacePage: async () => ({
    workspaceId: "ws-1",
    session: {
      userId: "u-1",
      workspaces: [{ id: "ws-1", name: "WS", role: "admin", state: "active" }],
    },
  }),
}));
vi.mock("@/lib/workspaces", () => ({ workspaceFetch }));
vi.mock("@/lib/target-api", () => ({ targetFetch: async () => ({ ok: true, data: { tokens: [] } }) }));
vi.mock("@/lib/session", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/session")>()),
  getSessionToken: async () => "tok",
}));

import SettingsPage from "./page";
import { Notice } from "@/components/ui/notice";
import { ConnectionResultBanner } from "@/components/dashboard/settings/connection-result-banner";
import { SUPERSEDED_COPY, parseConnectionResult, type ConnectionResult } from "@/lib/connection-result";

const RESULT_ID = "77777777-7777-4777-8777-777777777777";

/** `connect_choice.result`, as the API answers it. */
function applied(over: Record<string, unknown> = {}) {
  return {
    account_id: "55555555-5555-4555-8555-555555555555",
    source_id: "22222222-2222-4222-8222-222222222222",
    outcome: "connected_history_on",
    history_enabled: true,
    mix_revision: 10,
    mix_reset_applied: true,
    next_action: null,
    current_mix_revision: 10,
    created_at: "2026-10-12T14:00:00+00:00",
    ...over,
  };
}

function* walk(node: ReactNode): Generator<ReactElement> {
  if (Array.isArray(node)) {
    for (const child of node) yield* walk(child);
    return;
  }
  if (!isValidElement(node)) return;
  yield node;
  for (const value of Object.values(node.props as Record<string, unknown>)) {
    if (isValidElement(value) || Array.isArray(value)) yield* walk(value as ReactNode);
  }
}

function textOf(node: ReactNode): string {
  if (node === null || node === undefined || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join("");
  return textOf((node as ReactElement<{ children?: ReactNode }>).props?.children);
}

const ok = (data: unknown) => ({ ok: true, data });
let resultRead: unknown = ok(applied());

function answer(path: string) {
  if (path.startsWith("connection-results/")) return resultRead;
  switch (path) {
    case "":
      return ok({ id: "ws-1", name: "WS", state: "active", tz: "UTC" });
    case "accounts":
      return ok({ accounts: [] });
    case "sources":
      return ok({ sources: [] });
    case "stats":
      return ok({ intents_by_state: {}, media_by_state: {}, posts_by_day: [], accounts: 0, sources: 0 });
    case "drive":
      return ok({ drive: null });
    case "category-mix":
      return ok({ rows: [] });
  }
  return ok({});
}

async function page(params: Record<string, string>) {
  const tree = await SettingsPage({ searchParams: Promise.resolve(params) });
  const elements = [...walk(tree)];
  return {
    banners: elements.filter((el) => el.type === ConnectionResultBanner),
    notices: elements.filter((el) => el.type === Notice).map((el) => textOf(el)),
  };
}

function bannerText(result: ConnectionResult): string {
  return renderToStaticMarkup(createElement(ConnectionResultBanner, { result }))
    .replace(/<[^>]+>/g, " ")
    .replace(/&#x27;/g, "'")
    .replace(/\s+/g, " ")
    .trim();
}

function parsed(over: Record<string, unknown> = {}): ConnectionResult {
  const result = parseConnectionResult(applied(over));
  if (!result) throw new Error("fixture did not parse");
  return result;
}

beforeEach(() => {
  resultRead = ok(applied());
  workspaceFetch.mockReset();
  workspaceFetch.mockImplementation(async (path: string) => answer(path));
});

describe("the page reads the result the callback names", () => {
  it("reads it by its id and hands the banner what the API answered", async () => {
    resultRead = ok(applied({ outcome: "reconnected_preserved", history_enabled: false }));
    const { banners } = await page({ tab: "accounts", connection: RESULT_ID });
    expect(workspaceFetch.mock.calls.map(([path]) => path)).toContain(`connection-results/${RESULT_ID}`);
    expect(banners).toHaveLength(1);
    expect((banners[0].props as { result: ConnectionResult }).result).toEqual({
      outcome: "reconnected_preserved",
      historyEnabled: false,
      mixRevision: 10,
      currentMixRevision: 10,
    });
  });

  it("reads nothing for a query that is not an id, and shows no banner", async () => {
    for (const connection of ["connected_history_on", "../accounts", ""]) {
      const { banners } = await page({ connection });
      expect(banners, connection).toHaveLength(0);
    }
    expect(workspaceFetch.mock.calls.some(([path]) => String(path).startsWith("connection-results"))).toBe(false);
  });

  it("shows nothing extra for a result the API will not give: expired, or another session's", async () => {
    resultRead = { ok: false, status: 404, error: "http_404" };
    const { banners, notices } = await page({ connection: RESULT_ID });
    expect(banners).toHaveLength(0);
    expect(notices).toEqual([]);
  });

  it("shows nothing for an answer that is not a result", async () => {
    resultRead = ok(applied({ outcome: "connected_everything" }));
    expect((await page({ connection: RESULT_ID })).banners).toHaveLength(0);
  });

  it("gives the no-capability path its notice and no banner", async () => {
    const { banners, notices } = await page({ connected: "instagram" });
    expect(banners).toHaveLength(0);
    expect(notices).toEqual([
      "Instagram account connected. Storydump will keep its access fresh. Publishing through Instagram directly is not switched on yet; approvals still post by hand.",
    ]);
  });
});

describe("the banner says the outcome", () => {
  it.each([
    [
      "connected_history_on",
      true,
      "Instagram connected. Your past posts are now a content source — indexing has started.",
    ],
    [
      "connected_history_off",
      false,
      "Instagram connected for publishing. Past posts are Off; you can turn them on in Posting mix.",
    ],
    [
      "connected_history_off_mix_conflict",
      false,
      "Instagram connected. Past posts were left Off because the posting mix changed while you were connecting.",
    ],
    ["reconnected_preserved", true, "Instagram reconnected. Your past-posts setting was kept: On."],
    ["reconnected_preserved", false, "Instagram reconnected. Your past-posts setting was kept: Off."],
  ])("%s (past posts %s)", (outcome, history, sentence) => {
    const text = bannerText(parsed({ outcome, history_enabled: history }));
    expect(text.startsWith(sentence)).toBe(true);
    expect(text).not.toContain(SUPERSEDED_COPY);
  });

  it("says where to turn past posts on when a reconnect keeps them Off, and only then", () => {
    const off = bannerText(parsed({ outcome: "reconnected_preserved", history_enabled: false }));
    expect(off).toContain(
      "Instagram reconnected. Your past-posts setting was kept: Off. Turn past posts on in Posting mix.",
    );
    const on = bannerText(parsed({ outcome: "reconnected_preserved", history_enabled: true }));
    expect(on).toContain("Your past-posts setting was kept: On.");
    expect(on).not.toContain("Turn past posts on");
  });

  it("offers Review posting mix on the conflict, and links the current status on every other outcome", () => {
    const conflict = renderToStaticMarkup(
      createElement(ConnectionResultBanner, {
        result: parsed({ outcome: "connected_history_off_mix_conflict", history_enabled: false }),
      }),
    );
    expect(conflict).toMatch(/<a[^>]*href="\/dashboard\/settings\?tab=general#posting-mix"[^>]*>Review posting mix<\/a>/);
    const on = renderToStaticMarkup(createElement(ConnectionResultBanner, { result: parsed() }));
    expect(on).toMatch(/<a[^>]*href="\/dashboard\/settings\?tab=general#posting-mix"[^>]*>Open Posting mix<\/a>/);
    expect(on).not.toContain("Review posting mix");
  });

  it("says when the mix has moved on since the result, rather than presenting it as current", () => {
    const text = bannerText(parsed({ current_mix_revision: 11 }));
    expect(text).toContain(SUPERSEDED_COPY);
  });

  it("is a polite status", () => {
    const html = renderToStaticMarkup(createElement(ConnectionResultBanner, { result: parsed() }));
    expect(html).toMatch(/^<div role="status"/);
  });
});
