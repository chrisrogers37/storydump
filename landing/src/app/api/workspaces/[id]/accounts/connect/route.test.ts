/**
 * The two Instagram connect starts — the header's Add (`./route`) and a row's
 * connect (`../[accountId]/connect/route`) — as forwarders of the history
 * choice (#1644). The body the connect dialog sends reaches the API
 * unchanged, `{}` included (the empty start); a refusal keeps its reason, its
 * status and its revision; and the authorize-host guard still stands between
 * a 200 and the browser's navigation.
 *
 * `targetFetch` is the real one, against a stubbed `fetch`; only the session
 * is replaced.
 */

import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const WS = "11111111-1111-4111-8111-111111111111";
const ACCOUNT = "55555555-5555-4555-8555-555555555555";
const AUTHORIZE = "https://api.instagram.com/oauth/authorize?state=s";
const CHOICE = { past_posts_choice: { use_history: true, expected_mix_revision: 8, preview_version: 1 } };

type Upstream = { status: number; body: unknown };

const state: { token: string | null } = { token: "tok-test" };
let upstream: Upstream;
const sent: Array<{ url: string; init: RequestInit }> = [];

vi.mock("@/lib/session", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/session")>()),
  getSessionToken: async () => state.token,
}));

const { POST: ADD } = await import("./route");
const { POST: ROW } = await import("../[accountId]/connect/route");

function request(path: string, options: { body?: unknown; raw?: string; headers?: Record<string, string> }) {
  return new NextRequest(`https://storydump.app${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...options.headers },
    body: options.raw ?? JSON.stringify(options.body),
  });
}

function add(options: { body?: unknown; raw?: string; headers?: Record<string, string> } = { body: {} }) {
  return ADD(request(`/api/workspaces/${WS}/accounts/connect`, options), {
    params: Promise.resolve({ id: WS }),
  });
}

function row(
  options: { body?: unknown; raw?: string; headers?: Record<string, string> } = { body: {} },
  accountId = ACCOUNT,
) {
  return ROW(request(`/api/workspaces/${WS}/accounts/${accountId}/connect`, options), {
    params: Promise.resolve({ id: WS, accountId }),
  });
}

beforeEach(() => {
  sent.length = 0;
  state.token = "tok-test";
  upstream = { status: 200, body: { authorization_url: AUTHORIZE } };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: unknown, init: unknown) => {
      sent.push({ url: String(url), init: init as RequestInit });
      return new Response(JSON.stringify(upstream.body), {
        status: upstream.status,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the body reaches the API unchanged", () => {
  it.each([
    ["the empty start", {}],
    ["a history choice", CHOICE],
    ["no choice, said explicitly", { past_posts_choice: null }],
  ])("forwards %s from the header's Add", async (_label, body) => {
    const res = await add({ body });
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ authorizationUrl: AUTHORIZE });
    expect(sent).toHaveLength(1);
    expect(sent[0].url).toMatch(new RegExp(`/api/v1/workspaces/${WS}/accounts/connect$`));
    expect(sent[0].init.method).toBe("POST");
    expect(JSON.parse(String(sent[0].init.body))).toEqual(body);
  });

  it("forwards a row's reconnect, which carries no choice, on the row's own path", async () => {
    const res = await row({ body: { past_posts_choice: null } });
    expect(res.status).toBe(200);
    expect(sent[0].url).toMatch(new RegExp(`/api/v1/workspaces/${WS}/accounts/${ACCOUNT}/connect$`));
    expect(JSON.parse(String(sent[0].init.body))).toEqual({ past_posts_choice: null });
  });

  it("forwards a body the API will refuse: the choice's shape is the API's to judge", async () => {
    upstream = { status: 400, body: { detail: "past-posts choice refused", reason: "invalid_past_posts_choice" } };
    const res = await add({ body: { past_posts_choice: { use_history: "yes" } } });
    expect(res.status).toBe(400);
    expect(await res.json()).toEqual({ error: "invalid_past_posts_choice" });
    expect(JSON.parse(String(sent[0].init.body))).toEqual({ past_posts_choice: { use_history: "yes" } });
  });
});

describe("the checks the routes make themselves", () => {
  it.each([
    ["JSON declared as text", { raw: JSON.stringify(CHOICE), headers: { "Content-Type": "text/plain" } }],
    ["text that is not JSON", { raw: "{pastPosts" }],
    ["an empty body", { raw: "" }],
    ["null", { raw: "null" }],
    ["an array", { raw: JSON.stringify([CHOICE]) }],
  ])("answers 400 malformed_body for %s on both starts, and sends nothing", async (_label, options) => {
    for (const res of [await add(options), await row(options)]) {
      expect(res.status).toBe(400);
      expect(await res.json()).toEqual({ error: "malformed_body" });
    }
    expect(sent).toHaveLength(0);
  });

  it("refuses another site's request before anything else, and lets this site's through", async () => {
    const res = await add({ body: CHOICE, headers: { "sec-fetch-site": "cross-site" } });
    expect(res.status).toBe(403);
    expect(await res.json()).toEqual({ error: "cross_site" });
    expect(sent).toHaveLength(0);
    expect((await add({ body: CHOICE, headers: { "sec-fetch-site": "same-origin" } })).status).toBe(200);
  });

  it("refuses an account id that is not a UUID before reading the body", async () => {
    const res = await row({ raw: "not json" }, "nope");
    expect(res.status).toBe(400);
    expect(await res.json()).toEqual({ error: "invalid_account" });
    expect(sent).toHaveLength(0);
  });
});

describe("refusals and the redirect guard", () => {
  it("relays a stale choice's 409 with its revision", async () => {
    upstream = { status: 409, body: { reason: "mix_conflict", current_revision: 9 } };
    for (const res of [await add({ body: CHOICE }), await row({ body: CHOICE })]) {
      expect(res.status).toBe(409);
      expect(await res.json()).toEqual({ error: "mix_conflict", current_revision: 9 });
    }
  });

  it("relays a choice where past posts are not live by its own name", async () => {
    upstream = { status: 503, body: { detail: "past-posts choice refused", reason: "past_posts_not_activated" } };
    const res = await add({ body: CHOICE });
    expect(res.status).toBe(503);
    expect(await res.json()).toEqual({ error: "past_posts_not_activated" });
  });

  it("never hands the browser a URL off Instagram's authorize host", async () => {
    upstream = { status: 200, body: { authorization_url: "https://evil.example/oauth/authorize" } };
    for (const res of [await add({ body: CHOICE }), await row({ body: CHOICE })]) {
      expect(res.status).toBe(502);
      expect(await res.json()).toEqual({ error: "malformed_authorization_url" });
    }
  });
});
