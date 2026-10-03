/**
 * Sign out of all devices: `?everywhere=1` on the BFF's sign-out reaches the
 * API as `POST /auth/signout?everywhere=true`, and nothing else does.
 *
 * The plain sign-out must stay the plain one — a route that sent `everywhere`
 * on every call would sign a person out of their phone each time they signed
 * out of a shared laptop. And the local half is unchanged either way: both
 * cookies go and the browser lands on `/login`.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

const state: { token: string | null; apiOk: boolean } = {
  token: "tok-test",
  apiOk: true,
};
const captured: Array<{ path: string; init?: Record<string, unknown> }> = [];

vi.mock("@/lib/session", () => ({
  SESSION_COOKIE: "sd_session",
  WORKSPACE_COOKIE: "storydump_workspace",
  getSessionToken: async () => state.token,
}));

vi.mock("@/lib/target-api", () => ({
  targetFetch: async (
    path: string,
    _token: string | null,
    init?: Record<string, unknown>,
  ) => {
    captured.push({ path, init });
    return state.apiOk
      ? { ok: true, data: { signed_out: true } }
      : { ok: false, status: 503, error: "target_router_unreachable" };
  },
}));

const { POST } = await import("./route");

function post(query = "", headers: Record<string, string> = {}) {
  return new NextRequest(`https://storydump.app/api/auth/logout${query}`, {
    method: "POST",
    headers,
  });
}

beforeEach(() => {
  state.token = "tok-test";
  state.apiOk = true;
  captured.length = 0;
});

describe("sign out of all devices", () => {
  it("asks the API to revoke every session when everywhere=1", async () => {
    const response = await POST(post("?everywhere=1"));
    expect(captured).toEqual([
      { path: "/signout?everywhere=true", init: { method: "POST", plane: "auth" } },
    ]);
    expect(response.status).toBe(307);
    expect(response.headers.get("location")).toBe("https://storydump.app/login");
  });

  it("says so when the API could not sign the other devices out", async () => {
    state.apiOk = false;
    const response = await POST(post("?everywhere=1"));
    expect(response.headers.get("location")).toBe(
      "https://storydump.app/login?signout=incomplete",
    );
    // This browser is still signed out: the local half always happens.
    expect(response.headers.getSetCookie().join("\n")).toMatch(/sd_session=;/);
  });

  it("lands a plain sign-out on /login even when the API fails", async () => {
    state.apiOk = false;
    const response = await POST(post());
    expect(response.headers.get("location")).toBe("https://storydump.app/login");
  });

  it("revokes only this session without it", async () => {
    await POST(post());
    expect(captured.map((c) => c.path)).toEqual(["/signout"]);
  });

  it("does not read any other value as everywhere", async () => {
    await POST(post("?everywhere=true"));
    await POST(post("?everywhere=0"));
    expect(captured.map((c) => c.path)).toEqual(["/signout", "/signout"]);
  });

  it("clears both cookies either way", async () => {
    const response = await POST(post("?everywhere=1"));
    const cleared = response.headers.getSetCookie().join("\n");
    expect(cleared).toMatch(/sd_session=;/);
    expect(cleared).toMatch(/storydump_workspace=;/);
  });

  it("still calls nothing when there is no session", async () => {
    state.token = null;
    const response = await POST(post("?everywhere=1"));
    expect(captured).toEqual([]);
    expect(response.headers.get("location")).toBe("https://storydump.app/login");
  });

  it("refuses a cross-site post before it revokes anything", async () => {
    const response = await POST(post("?everywhere=1", { "sec-fetch-site": "same-site" }));
    expect(response.status).toBe(403);
    expect(captured).toEqual([]);
  });
});
