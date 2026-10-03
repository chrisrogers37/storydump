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

const state: { token: string | null; apiOk: boolean; revoked: number } = {
  token: "tok-test",
  apiOk: true,
  revoked: 2,
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
      ? { ok: true, data: { signed_out: true, revoked: state.revoked } }
      : { ok: false, status: 503, error: "target_router_unreachable" };
  },
}));

const { POST } = await import("./route");

function post(
  query = "",
  headers: Record<string, string> = {},
  origin = "https://storydump.app",
) {
  return new NextRequest(`${origin}/api/auth/logout${query}`, {
    method: "POST",
    headers,
  });
}

beforeEach(() => {
  state.token = "tok-test";
  state.apiOk = true;
  state.revoked = 2;
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

  it("says a plain sign-out was not confirmed when the API fails", async () => {
    state.apiOk = false;
    const response = await POST(post());
    expect(response.headers.get("location")).toBe(
      "https://storydump.app/login?signout=unconfirmed",
    );
    expect(response.headers.getSetCookie().join("\n")).toMatch(/sd_session=;/);
  });

  it("lands a plain sign-out on /login when it worked", async () => {
    const response = await POST(post());
    expect(response.headers.get("location")).toBe("https://storydump.app/login");
  });

  it("says nothing else was signed out when this session was already dead", async () => {
    state.revoked = 0;
    const response = await POST(post("?everywhere=1"));
    expect(response.headers.get("location")).toBe(
      "https://storydump.app/login?signout=stale",
    );
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

  it("still calls nothing when there is no session, and says so", async () => {
    state.token = null;
    const everywhere = await POST(post("?everywhere=1"));
    const plain = await POST(post());
    expect(captured).toEqual([]);
    expect(everywhere.headers.get("location")).toBe(
      "https://storydump.app/login?signout=stale",
    );
    expect(plain.headers.get("location")).toBe("https://storydump.app/login");
  });

  // The API sets the cookie with Domain=SESSION_COOKIE_DOMAIN, and a host-only
  // expiry does not replace it: the browser kept sending the session.
  it("expires the session cookie on this host and every parent domain", async () => {
    const response = await POST(post("", {}, "https://www.storydump.app"));
    const cleared = response.headers.getSetCookie();
    for (const domain of ["www.storydump.app", "storydump.app"]) {
      const cookie = cleared.find((c) => c.includes(`Domain=${domain};`));
      expect(cookie).toMatch(/^sd_session=; /);
      expect(cookie).toMatch(/Max-Age=0/);
      expect(cookie).toMatch(/Path=\//);
      expect(cookie).toMatch(/Secure/);
    }
    expect(cleared.some((c) => c.includes("Domain=app;"))).toBe(false);
    // The host-only deletes still go out alongside.
    expect(cleared.join("\n")).toMatch(/storydump_workspace=;/);
  });

  it("sets no Domain on localhost", async () => {
    const response = await POST(post("", {}, "http://localhost:3000"));
    expect(response.headers.getSetCookie().join("\n")).not.toMatch(/Domain=/);
  });

  it("refuses a cross-site post before it revokes anything", async () => {
    const response = await POST(post("?everywhere=1", { "sec-fetch-site": "same-site" }));
    expect(response.status).toBe(403);
    expect(captured).toEqual([]);
  });
});
