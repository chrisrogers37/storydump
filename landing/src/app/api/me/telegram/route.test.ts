/**
 * `DELETE /api/me/telegram` — the person's own Telegram unlink (094, `07` §37).
 *
 * Pinned: no session reaches nothing; the call is a DELETE at `/me/telegram`;
 * only an outcome the API names is confirmed; a refusal (`last_identity`) is
 * reduced to its reason and status.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

const state: { token: string | null; upstream: unknown } = {
  token: "tok-test",
  upstream: { ok: true, data: {} },
};
const captured: Array<{ path: string; init?: Record<string, unknown> }> = [];

vi.mock("@/lib/session", () => ({
  getSessionToken: async () => state.token,
}));

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

function del() {
  return DELETE(
    new Request("https://storydump.app/api/me/telegram", {
      method: "DELETE",
    }) as unknown as Parameters<typeof DELETE>[0],
  );
}

beforeEach(() => {
  captured.length = 0;
  state.token = "tok-test";
  state.upstream = { ok: true, data: {} };
});

describe("DELETE /api/me/telegram", () => {
  it("answers 401 without a session and reaches nothing", async () => {
    state.token = null;
    expect((await del()).status).toBe(401);
    expect(captured).toHaveLength(0);
  });

  it.each(["unlinked", "not_linked"])(
    "deletes at /me/telegram and passes the outcome %s through",
    async (outcome) => {
      state.upstream = { ok: true, data: { outcome } };
      const res = await del();
      expect(res.status).toBe(200);
      expect(captured[0].path).toBe("/me/telegram");
      expect(captured[0].init?.method).toBe("DELETE");
      expect(await res.json()).toEqual({ outcome });
    },
  );

  it("does not invent a confirmation the API did not give", async () => {
    state.upstream = { ok: true, data: { outcome: "maybe" } };
    const res = await del();
    expect(res.status).toBe(502);
    expect(await res.json()).toEqual({ error: "malformed_response" });
  });

  it("reduces the last-identity refusal to its reason and status", async () => {
    state.upstream = { ok: false, status: 409, error: "last_identity" };
    const res = await del();
    expect(res.status).toBe(409);
    expect(await res.json()).toEqual({ error: "last_identity" });
  });
});
