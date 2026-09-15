/**
 * `/api/workspaces/[id]/tokens` — the workspace service identity's proxy
 * (CLI v2 phase 01, step 10; spec §2).
 *
 * The one thing this route must NOT do is forward a role. A service identity
 * reads only in this release — the API fixes `readonly` and its body has no
 * `role` field — so a route that passed one through would be the first
 * place a write-capable service principal could be asked for, one release
 * before anything authorises it. The body is `{name, expires_in_days}` and
 * that is pinned.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

const WS = "11111111-1111-4111-8111-111111111111";
const TOK = "22222222-2222-4222-8222-222222222222";
const SECRET = "sdt_" + "c".repeat(43);

const state: { token: string | null; upstream: unknown } = {
  token: "tok-test",
  upstream: { ok: true, data: {} },
};
const captured: Array<{ path: string; init?: Record<string, unknown> }> = [];

vi.mock("@/lib/session", () => {
  const UUID =
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  return {
    getSessionToken: async () => state.token,
    isUuid: (v: unknown) => typeof v === "string" && UUID.test(v),
    isWorkspaceId: (v: unknown) => typeof v === "string" && UUID.test(v),
  };
});

vi.mock("@/lib/target-api", () => ({
  targetFetch: async (
    path: string,
    _token: string | null,
    init?: Record<string, unknown>,
  ) => {
    captured.push({ path, init });
    return state.upstream;
  },
}));

const { POST } = await import("./route");
const { DELETE } = await import("./[tokenId]/route");

function ctx(id: string) {
  return { params: Promise.resolve({ id }) };
}

function post(body: unknown, id = WS) {
  const request = new Request(
    `https://storydump.app/api/workspaces/${id}/tokens`,
    {
      method: "POST",
      body: typeof body === "string" ? body : JSON.stringify(body),
    },
  ) as unknown as Parameters<typeof POST>[0];
  return POST(request, ctx(id));
}

function del(tokenId: string, id = WS) {
  const request = new Request(
    `https://storydump.app/api/workspaces/${id}/tokens/${tokenId}`,
    {
      method: "DELETE",
    },
  ) as unknown as Parameters<typeof DELETE>[0];
  return DELETE(request, { params: Promise.resolve({ id, tokenId }) });
}

const API_ROW = {
  id: TOK,
  name: "ci",
  role: "readonly",
  workspace_id: WS,
  expires_at: "2026-12-14T00:00:00Z",
  revoked_at: null,
  last_used_at: null,
  created_at: "2026-09-15T00:00:00Z",
};

beforeEach(() => {
  captured.length = 0;
  state.token = "tok-test";
  state.upstream = { ok: true, data: {} };
});

describe("the gates every verb shares", () => {
  it("without a session: 401, and nothing reaches the port", async () => {
    state.token = null;
    expect((await post({ name: "ci", expires_in_days: 90 })).status).toBe(401);
    expect((await del(TOK)).status).toBe(401);
    expect(captured).toHaveLength(0);
  });

  it("a workspace id that is not a UUID: 400, and nothing reaches the port", async () => {
    for (const res of [
      await post({ name: "ci", expires_in_days: 90 }, "nope"),
      await del(TOK, "nope"),
    ]) {
      expect(res.status).toBe(400);
      expect((await res.json()).error).toBe("invalid_workspace");
    }
    expect(captured).toHaveLength(0);
  });
});

describe("POST /api/workspaces/[id]/tokens", () => {
  it("forwards {name, expires_in_days} and NOTHING else — no role, even if one was sent", async () => {
    state.upstream = { ok: true, data: { ...API_ROW, secret: SECRET } };
    const res = await post({
      name: " ci ",
      expires_in_days: 30,
      role: "operator",
    });
    expect(res.status).toBe(201);
    expect(captured[0].path).toBe(`/workspaces/${WS}/tokens`);
    expect(captured[0].init?.method).toBe("POST");
    expect(JSON.parse(String(captured[0].init?.body))).toEqual({
      name: "ci",
      expires_in_days: 30,
    });
  });

  it("reshapes the 201 to camelCase, with the workspace and the secret", async () => {
    state.upstream = { ok: true, data: { ...API_ROW, secret: SECRET } };
    const res = await post({ name: "ci", expires_in_days: 90 });
    expect(await res.json()).toEqual({
      id: TOK,
      name: "ci",
      role: "readonly",
      expiresAt: "2026-12-14T00:00:00Z",
      secret: SECRET,
      workspaceId: WS,
    });
  });

  it("refuses a bad body locally, without reaching the port", async () => {
    const cases: Array<[unknown, string]> = [
      ["{not json", "malformed_body"],
      [{ expires_in_days: 90 }, "invalid_name"],
      [{ name: "a".repeat(81), expires_in_days: 90 }, "invalid_name"],
      [{ name: "ci", expires_in_days: 0 }, "invalid_expiry"],
      [{ name: "ci", expires_in_days: 400 }, "invalid_expiry"],
      [{ name: "ci", expires_in_days: "90" }, "invalid_expiry"],
    ];
    for (const [body, error] of cases) {
      const res = await post(body);
      expect(res.status, JSON.stringify(body)).toBe(400);
      expect((await res.json()).error, JSON.stringify(body)).toBe(error);
    }
    expect(captured).toHaveLength(0);
  });

  it("reduces a member's 403 to its reason and status — the copy names the floor from that", async () => {
    state.upstream = { ok: false, status: 403, error: "insufficient_role" };
    const res = await post({ name: "ci", expires_in_days: 90 });
    expect(res.status).toBe(403);
    expect(await res.json()).toEqual({ error: "insufficient_role" });

    // The API's plain `{detail: "forbidden"}` has no reason; the client
    // synthesises `http_403` and that is what reaches the browser.
    state.upstream = { ok: false, status: 403, error: "http_403" };
    expect(
      await (await post({ name: "ci", expires_in_days: 90 })).json(),
    ).toEqual({ error: "http_403" });
  });

  it("a 201 without a secret is a 502", async () => {
    state.upstream = { ok: true, data: { ...API_ROW } };
    const res = await post({ name: "ci", expires_in_days: 90 });
    expect(res.status).toBe(502);
    expect((await res.json()).error).toBe("malformed_response");
  });
});

describe("DELETE /api/workspaces/[id]/tokens/[tokenId]", () => {
  it("refuses a token id that is not a UUID without reaching the port", async () => {
    const res = await del("nope");
    expect(res.status).toBe(400);
    expect((await res.json()).error).toBe("invalid_token");
    expect(captured).toHaveLength(0);
  });

  it("deletes at /workspaces/{ws}/tokens/{id} and confirms what the API confirmed", async () => {
    state.upstream = { ok: true, data: { revoked: true } };
    const res = await del(TOK);
    expect(res.status).toBe(200);
    expect(captured[0].path).toBe(`/workspaces/${WS}/tokens/${TOK}`);
    expect(captured[0].init?.method).toBe("DELETE");
    expect(await res.json()).toEqual({ tokenId: TOK, revoked: true });
  });

  it("reduces a refusal to its reason and status", async () => {
    state.upstream = { ok: false, status: 404, error: "http_404" };
    const res = await del(TOK);
    expect(res.status).toBe(404);
    expect(await res.json()).toEqual({ error: "http_404" });
  });

  it("does not invent a confirmation the API did not give", async () => {
    state.upstream = { ok: true, data: {} };
    const res = await del(TOK);
    expect(res.status).toBe(502);
    expect(await res.json()).toEqual({ error: "malformed_response" });
  });
});
