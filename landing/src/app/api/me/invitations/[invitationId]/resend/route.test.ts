/**
 * `POST /api/me/invitations/[invitationId]/resend` — "Email me the link again"
 * (115).
 *
 * Pinned: another site's request and no session reach nothing; only an id's
 * shape goes on to the API; the answer is the email's delivery and never a
 * link; the API's 404 for an invitation that is not theirs passes through.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

const INVITATION = "11111111-1111-4111-8111-111111111111";
const state: { token: string | null; upstream: unknown } = {
  token: "tok-test",
  upstream: { ok: true, data: {} },
};
const captured: Array<{ path: string; init?: Record<string, unknown> }> = [];

vi.mock("@/lib/session", async (actual) => ({
  ...(await actual<typeof import("@/lib/session")>()),
  getSessionToken: async () => state.token,
}));

vi.mock("@/lib/target-api", () => ({
  targetFetch: async (path: string, _token: string | null, init?: Record<string, unknown>) => {
    captured.push({ path, init });
    return state.upstream;
  },
}));

const { POST } = await import("./route");

function post(id: string, headers: Record<string, string> = {}) {
  const request = new Request(`https://storydump.app/api/me/invitations/${id}/resend`, {
    method: "POST",
    headers,
  });
  return POST(request as unknown as Parameters<typeof POST>[0], {
    params: Promise.resolve({ invitationId: id }),
  });
}

beforeEach(() => {
  captured.length = 0;
  state.token = "tok-test";
  state.upstream = { ok: true, data: { delivery: { channel: "email", state: "sent" } } };
});

describe("POST /api/me/invitations/[invitationId]/resend", () => {
  it("refuses another site's request before anything else", async () => {
    const res = await post(INVITATION, { "sec-fetch-site": "cross-site" });
    expect(res.status).toBe(403);
    expect(captured).toHaveLength(0);
  });

  it("answers 401 without a session and reaches nothing", async () => {
    state.token = null;
    expect((await post(INVITATION)).status).toBe(401);
    expect(captured).toHaveLength(0);
  });

  it("refuses an id that is not an id's shape before the API", async () => {
    const res = await post("../../workspaces");
    expect(res.status).toBe(400);
    expect(await res.json()).toEqual({ error: "invalid_invitation" });
    expect(captured).toHaveLength(0);
  });

  it("posts to the person's own invitation and answers its delivery alone", async () => {
    state.upstream = {
      ok: true,
      data: { delivery: { channel: "email", state: "sent" }, join_url: "https://x/join/t" },
    };
    const res = await post(INVITATION);
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ delivery: { channel: "email", state: "sent" } });
    expect(captured).toEqual([
      { path: `/me/invitations/${INVITATION}/resend`, init: { method: "POST" } },
    ]);
  });

  it("passes the API's refusal through, reason and status", async () => {
    state.upstream = { ok: false, status: 404, error: "not_acceptable" };
    const res = await post(INVITATION);
    expect(res.status).toBe(404);
    expect(await res.json()).toEqual({ error: "not_acceptable" });
  });
});
