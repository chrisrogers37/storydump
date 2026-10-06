/**
 * `DELETE /api/workspaces/[id]/bindings/[bindingId]` — removing a Telegram
 * group (`07` §13). The proxy validates both ids, forwards a bare DELETE, and
 * hands a refusal back as its reason and status for the card's copy.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

const WS = "11111111-1111-4111-8111-111111111111";
const BINDING = "44444444-4444-4444-8444-444444444444";

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

const { DELETE } = await import("./route");

function del(bindingId: string, id = WS, headers: Record<string, string> = {}) {
  const request = new Request(
    `https://storydump.app/api/workspaces/${id}/bindings/${bindingId}`,
    { method: "DELETE", headers },
  ) as unknown as Parameters<typeof DELETE>[0];
  return DELETE(request, { params: Promise.resolve({ id, bindingId }) });
}

beforeEach(() => {
  captured.length = 0;
  state.token = "tok-test";
  state.upstream = { ok: true, data: {} };
});

describe("DELETE /api/workspaces/[id]/bindings/[bindingId]", () => {
  it("without a session: 401, and nothing reaches the port", async () => {
    state.token = null;
    expect((await del(BINDING)).status).toBe(401);
    expect(captured).toHaveLength(0);
  });

  it("refuses a cross-site request without reaching the port", async () => {
    const res = await del(BINDING, WS, { "sec-fetch-site": "cross-site" });
    expect(res.status).toBe(403);
    expect(captured).toHaveLength(0);
  });

  it("refuses ids that are not UUIDs without reaching the port", async () => {
    const badWs = await del(BINDING, "nope");
    expect(badWs.status).toBe(400);
    expect((await badWs.json()).error).toBe("invalid_workspace");
    const badBinding = await del("nope");
    expect(badBinding.status).toBe(400);
    expect((await badBinding.json()).error).toBe("invalid_binding");
    expect(captured).toHaveLength(0);
  });

  it("deletes at /workspaces/{ws}/bindings/{id} and says what the API said", async () => {
    state.upstream = {
      ok: true,
      data: { binding_id: BINDING, state: "revoked" },
    };
    const res = await del(BINDING);
    expect(res.status).toBe(200);
    expect(captured[0].path).toBe(`/workspaces/${WS}/bindings/${BINDING}`);
    expect(captured[0].init?.method).toBe("DELETE");
    expect(await res.json()).toEqual({ bindingId: BINDING, state: "revoked" });
  });

  it("reduces a refusal to its reason and status", async () => {
    for (const [status, error] of [
      [404, "http_404"],
      [403, "http_403"],
    ] as const) {
      state.upstream = { ok: false, status, error };
      const res = await del(BINDING);
      expect(res.status).toBe(status);
      expect(await res.json()).toEqual({ error });
    }
  });
});
