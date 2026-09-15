/**
 * `/api/me/tokens` — the person-bound token's proxy (CLI v2 phase 01, step 10).
 *
 * Three things are pinned, and each is a way this tier has been wrong before:
 *
 *   1. A bad body never reaches the port. The shape is checked HERE — name,
 *      role, expiry — so a blank name is a field error next to the input and
 *      not a 400 from upstream that `mintTokenRefusalCopy` can only half-name.
 *   2. The reshape is snake_case in, camelCase out, and the SECRET survives it.
 *      A route that dropped `secret` would return a 201 with nothing to paste:
 *      a token minted, a person who cannot use it, and no error anywhere.
 *   3. An upstream failure is reduced to `{error: reason}` with the upstream
 *      status. The raw body is never forwarded — `target-api.ts` says why.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

const TOK = "22222222-2222-4222-8222-222222222222";
const SECRET = "sdt_" + "b".repeat(43);

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

function post(body: unknown) {
  return new Request("https://storydump.app/api/me/tokens", {
    method: "POST",
    body: typeof body === "string" ? body : JSON.stringify(body),
  }) as unknown as Parameters<typeof POST>[0];
}

function del(tokenId: string) {
  const request = new Request(
    `https://storydump.app/api/me/tokens/${tokenId}`,
    {
      method: "DELETE",
    },
  ) as unknown as Parameters<typeof DELETE>[0];
  return DELETE(request, { params: Promise.resolve({ tokenId }) });
}

const API_ROW = {
  id: TOK,
  name: "laptop",
  role: "operator",
  expires_at: "2026-12-14T00:00:00Z",
  revoked_at: null,
  last_used_at: "2026-09-15T01:00:00Z",
  created_at: "2026-09-15T00:00:00Z",
};

beforeEach(() => {
  captured.length = 0;
  state.token = "tok-test";
  state.upstream = { ok: true, data: {} };
});

describe("without a session", () => {
  it("every verb answers 401 and nothing reaches the port", async () => {
    state.token = null;
    expect(
      (
        await POST(
          post({ name: "laptop", role: "operator", expires_in_days: 90 }),
        )
      ).status,
    ).toBe(401);
    expect((await del(TOK)).status).toBe(401);
    expect(captured).toHaveLength(0);
  });
});

describe("POST /api/me/tokens", () => {
  it("forwards the API's body exactly, name trimmed, to /me/tokens", async () => {
    state.upstream = {
      ok: true,
      data: {
        id: TOK,
        name: "laptop",
        role: "operator",
        expires_at: "2026-12-14T00:00:00Z",
        secret: SECRET,
      },
    };
    const res = await POST(
      post({ name: "  laptop ", role: "operator", expires_in_days: 90 }),
    );
    expect(res.status).toBe(201);
    expect(captured[0].path).toBe("/me/tokens");
    expect(captured[0].init?.method).toBe("POST");
    expect(JSON.parse(String(captured[0].init?.body))).toEqual({
      name: "laptop",
      role: "operator",
      expires_in_days: 90,
    });
  });

  it("reshapes the 201 to camelCase and keeps the secret", async () => {
    state.upstream = {
      ok: true,
      data: {
        id: TOK,
        name: "laptop",
        role: "operator",
        expires_at: "2026-12-14T00:00:00Z",
        secret: SECRET,
      },
    };
    const res = await POST(
      post({ name: "laptop", role: "operator", expires_in_days: 90 }),
    );
    expect(await res.json()).toEqual({
      id: TOK,
      name: "laptop",
      role: "operator",
      expiresAt: "2026-12-14T00:00:00Z",
      secret: SECRET,
      workspaceId: null,
    });
  });

  it("defaults the expiry to 90 days when the body leaves it out", async () => {
    state.upstream = {
      ok: true,
      data: {
        id: TOK,
        name: "laptop",
        role: "readonly",
        expires_at: null,
        secret: SECRET,
      },
    };
    await POST(post({ name: "laptop", role: "readonly" }));
    expect(JSON.parse(String(captured[0].init?.body)).expires_in_days).toBe(90);
  });

  it("refuses a bad body locally, by field, without reaching the port", async () => {
    const cases: Array<[unknown, string]> = [
      ["{not json", "malformed_body"],
      [{ role: "operator", expires_in_days: 90 }, "invalid_name"],
      [{ name: "   ", role: "operator", expires_in_days: 90 }, "invalid_name"],
      [
        { name: "a".repeat(81), role: "operator", expires_in_days: 90 },
        "invalid_name",
      ],
      [{ name: "laptop", role: "admin", expires_in_days: 90 }, "invalid_role"],
      [{ name: "laptop", expires_in_days: 90 }, "invalid_role"],
      [
        { name: "laptop", role: "operator", expires_in_days: 0 },
        "invalid_expiry",
      ],
      [
        { name: "laptop", role: "operator", expires_in_days: 366 },
        "invalid_expiry",
      ],
      [
        { name: "laptop", role: "operator", expires_in_days: 1.5 },
        "invalid_expiry",
      ],
      [
        { name: "laptop", role: "operator", expires_in_days: "90" },
        "invalid_expiry",
      ],
    ];
    for (const [body, error] of cases) {
      const res = await POST(post(body));
      expect(res.status, JSON.stringify(body)).toBe(400);
      expect((await res.json()).error, JSON.stringify(body)).toBe(error);
    }
    expect(captured).toHaveLength(0);
  });

  it("reduces an upstream refusal to its reason and status, nothing else", async () => {
    state.upstream = { ok: false, status: 400, error: "invalid_args" };
    const res = await POST(
      post({ name: "laptop", role: "operator", expires_in_days: 90 }),
    );
    expect(res.status).toBe(400);
    expect(await res.json()).toEqual({ error: "invalid_args" });
  });

  it("treats a 201 with no usable secret as a failure, not a success", async () => {
    state.upstream = {
      ok: true,
      data: { id: TOK, name: "laptop", role: "operator", expires_at: null },
    };
    const res = await POST(
      post({ name: "laptop", role: "operator", expires_in_days: 90 }),
    );
    expect(res.status).toBe(502);
    expect((await res.json()).error).toBe("malformed_response");

    state.upstream = { ok: true, data: { ...API_ROW, secret: "not-a-token" } };
    const odd = await POST(
      post({ name: "laptop", role: "operator", expires_in_days: 90 }),
    );
    expect(odd.status).toBe(502);
  });
});

describe("DELETE /api/me/tokens/[tokenId]", () => {
  it("refuses a token id that is not a UUID without reaching the port", async () => {
    const res = await del("not-a-uuid");
    expect(res.status).toBe(400);
    expect((await res.json()).error).toBe("invalid_token");
    expect(captured).toHaveLength(0);
  });

  it("deletes at /me/tokens/{id} and confirms only what the API confirmed", async () => {
    state.upstream = { ok: true, data: { revoked: true } };
    const res = await del(TOK);
    expect(res.status).toBe(200);
    expect(captured[0].path).toBe(`/me/tokens/${TOK}`);
    expect(captured[0].init?.method).toBe("DELETE");
    expect(await res.json()).toEqual({ tokenId: TOK, revoked: true });
  });

  it("does not invent a confirmation the API did not give", async () => {
    state.upstream = { ok: true, data: {} };
    const res = await del(TOK);
    expect(res.status).toBe(502);
    expect(await res.json()).toEqual({ error: "malformed_response" });
  });

  it("reduces a 404 — not the caller's token — to its reason and status", async () => {
    state.upstream = { ok: false, status: 404, error: "http_404" };
    const res = await del(TOK);
    expect(res.status).toBe(404);
    expect(await res.json()).toEqual({ error: "http_404" });
  });
});
