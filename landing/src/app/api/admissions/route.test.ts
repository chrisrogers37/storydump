/**
 * `POST /api/admissions` — the owner admits an address, or emails it again
 * (#1723).
 *
 * Pinned: another site's request and no session reach nothing; the body's
 * shape is checked first (one address of at most 254 characters, and a
 * boolean), and only those two fields go on to the API; the API's answer and
 * its refusals pass through, reason and status; a 2xx it could not read, or
 * one that names no outcome, is a 502 `malformed_response` (#1740).
 * `route-chain.test.ts` runs the same route between the real clients.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

const state: { token: string | null; upstream: unknown } = {
  token: "tok-test",
  upstream: { ok: true, data: { outcome: "admitted" } },
};
const captured: Array<{ path: string; token: string | null; init?: Record<string, unknown> }> = [];

vi.mock("@/lib/session", async (actual) => ({
  ...(await actual<typeof import("@/lib/session")>()),
  getSessionToken: async () => state.token,
}));

vi.mock("@/lib/target-api", () => ({
  targetFetch: async (path: string, token: string | null, init?: Record<string, unknown>) => {
    captured.push({ path, token, init });
    return state.upstream;
  },
}));

const { POST } = await import("./route");

function post(body: string, headers: Record<string, string> = {}) {
  const request = new Request("https://storydump.app/api/admissions", {
    method: "POST",
    headers: { "content-type": "application/json", ...headers },
    body,
  });
  return POST(request as unknown as Parameters<typeof POST>[0]);
}

const json = (value: unknown) => post(JSON.stringify(value));

/** The body that reached the API. */
const forwarded = () => JSON.parse(captured[0].init?.body as string);

beforeEach(() => {
  captured.length = 0;
  state.token = "tok-test";
  state.upstream = { ok: true, data: { outcome: "admitted" } };
});

describe("POST /api/admissions", () => {
  it("refuses another site's request before anything else", async () => {
    const res = await post(JSON.stringify({ email: "a@example.com", resend: false }), {
      "sec-fetch-site": "cross-site",
    });
    expect(res.status).toBe(403);
    expect(await res.json()).toEqual({ error: "cross_site" });
    expect(captured).toHaveLength(0);
  });

  it("answers 401 without a session and reaches nothing", async () => {
    state.token = null;
    const res = await json({ email: "a@example.com", resend: false });
    expect(res.status).toBe(401);
    expect(await res.json()).toEqual({ error: "unauthenticated" });
    expect(captured).toHaveLength(0);
  });

  it("posts the address and the boolean, and nothing else, with the session", async () => {
    const res = await json({ email: "  person@example.com ", resend: true, role: "owner" });
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ outcome: "admitted" });
    expect(captured).toHaveLength(1);
    expect(captured[0].path).toBe("/admissions");
    expect(captured[0].token).toBe("tok-test");
    expect(captured[0].init?.method).toBe("POST");
    expect(forwarded()).toEqual({ email: "person@example.com", resend: true });
  });

  it("takes an address of exactly 254 characters, counted as characters", async () => {
    // Two UTF-16 units each, so a bound on `.length` would refuse it.
    const longest = "𝒶".repeat(242) + "@example.com";
    expect([...longest]).toHaveLength(254);
    expect(longest.length).toBeGreaterThan(254);
    expect((await json({ email: longest, resend: false })).status).toBe(200);
    expect(forwarded().email).toBe(longest);
  });

  it.each([
    ["no JSON at all", "not json"],
    ["a list", JSON.stringify([{ email: "a@example.com", resend: false }])],
    ["null", "null"],
    ["a string", JSON.stringify("a@example.com")],
    ["no resend", JSON.stringify({ email: "a@example.com" })],
    ["a resend that is not a boolean", JSON.stringify({ email: "a@example.com", resend: "true" })],
  ])("refuses %s as a malformed body, before the API", async (_why, body) => {
    const res = await post(body);
    expect(res.status).toBe(400);
    expect(await res.json()).toEqual({ error: "malformed_body" });
    expect(captured).toHaveLength(0);
  });

  it("refuses a body not declared as JSON", async () => {
    const res = await post(JSON.stringify({ email: "a@example.com", resend: false }), {
      "content-type": "text/plain",
    });
    expect(res.status).toBe(400);
    expect(captured).toHaveLength(0);
  });

  it.each([
    ["no address", { resend: false }],
    ["an address that is not a string", { email: ["a@example.com"], resend: false }],
    ["an empty address", { email: "   ", resend: false }],
    ["an address past 254 characters", { email: "a".repeat(243) + "@example.com", resend: false }],
  ])("refuses %s as invalid_email, before the API", async (_why, body) => {
    const res = await json(body);
    expect(res.status).toBe(400);
    expect(await res.json()).toEqual({ error: "invalid_email" });
    expect(captured).toHaveLength(0);
  });

  it.each([
    [409, "signed_up"],
    [400, "invalid_email"],
    [403, "not_ops"],
    [503, "not_configured"],
  ])("passes the API's %s %s through, reason and status", async (status, error) => {
    state.upstream = { ok: false, status, error };
    const res = await json({ email: "a@example.com", resend: false });
    expect(res.status).toBe(status);
    expect(await res.json()).toEqual({ error });
  });

  it("answers the outcome alone", async () => {
    state.upstream = { ok: true, data: { outcome: "resent", job_id: "j-1" } };
    expect(await (await json({ email: "a@example.com", resend: true })).json()).toEqual({
      outcome: "resent",
    });
  });

  it.each([200, 202])(
    "answers a %s the client could not read as a 502, never as a success",
    async (status) => {
      state.upstream = { ok: false, status, error: "malformed_response" };
      const res = await json({ email: "a@example.com", resend: false });
      expect(res.status).toBe(502);
      expect(await res.json()).toEqual({ error: "malformed_response" });
    },
  );

  it("passes a refusal through with its own status, whatever its reason", async () => {
    state.upstream = { ok: false, status: 500, error: "malformed_response" };
    const res = await json({ email: "a@example.com", resend: false });
    expect(res.status).toBe(500);
    expect(await res.json()).toEqual({ error: "malformed_response" });
  });

  it.each([
    ["no body", undefined],
    ["null", null],
    ["a list", [{ outcome: "admitted" }]],
    ["a bare string", "admitted"],
    ["no outcome", { job_id: "j-1" }],
    ["an outcome it does not know", { outcome: "toString" }],
  ])("answers a success carrying %s as a 502, without a throw", async (_why, data) => {
    state.upstream = { ok: true, data };
    const res = await json({ email: "a@example.com", resend: false });
    expect(res.status).toBe(502);
    expect(await res.json()).toEqual({ error: "malformed_response" });
    expect(captured).toHaveLength(1);
  });
});
