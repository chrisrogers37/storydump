/**
 * `GET /api/workspaces/[id]/accounts/connect-preview` — what an Instagram
 * connect would do, asked before it starts (#1644). A forwarder: the
 * optional `account_id` reaches the API as the browser sent it, the answer
 * comes back whole, and a refusal keeps its reason and status. `targetFetch`
 * is the real one, against a stubbed `fetch`; only the session is replaced.
 */

import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const WS = "11111111-1111-4111-8111-111111111111";
const ACCOUNT = "55555555-5555-4555-8555-555555555555";
const DRIVE = "33333333-3333-4333-8333-333333333333";

/** `connect_choice.preview`, whole: the route must not drop or reshape a key. */
const PREVIEW = {
  past_posts_available: true,
  entry: "first_connect",
  account_id: ACCOUNT,
  handle: "example_handle",
  history: { editable: true, default: true, saved_enabled: null },
  expected_mix_revision: 8,
  preview_version: 1,
  reset: {
    applies: true,
    mode_after: "equal",
    rows_after: [
      { source_id: null, provider: "instagram_past_posts", name: "example_handle", target_share: "0.5" },
      { source_id: DRIVE, provider: "gdrive", name: "Event photos", target_share: "0.5" },
    ],
  },
};

const state: { token: string | null } = { token: "tok-test" };
let upstream: { status: number; body: unknown } | { unreachable: true };
const sent: string[] = [];

vi.mock("@/lib/session", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/session")>()),
  getSessionToken: async () => state.token,
}));

const { GET } = await import("./route");

function ask(query = "", id = WS) {
  const request = new NextRequest(
    `https://storydump.app/api/workspaces/${id}/accounts/connect-preview${query}`,
  );
  return GET(request, { params: Promise.resolve({ id }) });
}

beforeEach(() => {
  sent.length = 0;
  state.token = "tok-test";
  upstream = { status: 200, body: PREVIEW };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: unknown) => {
      sent.push(String(url));
      if ("unreachable" in upstream) throw new TypeError("fetch failed");
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

describe("the connect preview's forwarder", () => {
  it("previews the header's Add with no query, and a row's connect with its account", async () => {
    expect((await ask()).status).toBe(200);
    expect((await ask(`?account_id=${ACCOUNT}`)).status).toBe(200);
    expect(sent).toHaveLength(2);
    expect(sent[0]).toMatch(new RegExp(`/api/v1/workspaces/${WS}/accounts/connect-preview$`));
    expect(sent[1]).toMatch(
      new RegExp(`/api/v1/workspaces/${WS}/accounts/connect-preview\\?account_id=${ACCOUNT}$`),
    );
  });

  it("returns the answer whole", async () => {
    expect(await (await ask(`?account_id=${ACCOUNT}`)).json()).toEqual(PREVIEW);
  });

  it("refuses an account that is not a UUID, and sends nothing", async () => {
    for (const query of ["?account_id=nope", "?account_id=", `?account_id=${ACCOUNT}0`]) {
      const res = await ask(query);
      expect(res.status, query).toBe(400);
      expect(await res.json()).toEqual({ error: "invalid_account" });
    }
    expect(sent).toHaveLength(0);
  });

  it("answers 401 without a session and 400 for a workspace that is not an id, sending nothing", async () => {
    state.token = null;
    expect(await (await ask()).json()).toEqual({ error: "unauthenticated" });
    state.token = "tok-test";
    const res = await ask("", "nope");
    expect(res.status).toBe(400);
    expect(await res.json()).toEqual({ error: "invalid_workspace" });
    expect(sent).toHaveLength(0);
  });

  it("relays a refusal by its reason and status", async () => {
    upstream = { status: 403, body: { detail: "admin required", reason: "insufficient_role" } };
    let res = await ask();
    expect(res.status).toBe(403);
    expect(await res.json()).toEqual({ error: "insufficient_role" });

    upstream = { status: 404, body: { detail: "not found" } };
    res = await ask(`?account_id=${ACCOUNT}`);
    expect(res.status).toBe(404);
    expect(await res.json()).toEqual({ error: "http_404" });

    upstream = { unreachable: true };
    res = await ask();
    expect(res.status).toBe(503);
    expect(await res.json()).toEqual({ error: "target_router_unreachable" });
  });
});
