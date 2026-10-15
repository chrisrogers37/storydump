/**
 * `listMyInvitations` (invitee invitation functions): the read behind the invitee's panel. "We couldn't
 * check" and "there are none" are different screens, so a read that could not
 * be made or answered is null, never `[]`.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

const state: { token: string | null; upstream: unknown } = {
  token: "tok-test",
  upstream: { ok: true, data: { invitations: [] } },
};
const asked: string[] = [];

vi.mock("./session", async (actual) => ({
  ...(await actual<typeof import("./session")>()),
  getSessionToken: async () => state.token,
}));

vi.mock("./target-api", () => ({
  targetFetch: async (path: string) => {
    asked.push(path);
    return state.upstream;
  },
}));

const { listMyInvitations } = await import("./workspaces");

beforeEach(() => {
  asked.length = 0;
  state.token = "tok-test";
  state.upstream = { ok: true, data: { invitations: [] } };
});

describe("the invitations read", () => {
  it("asks the API for the person's own and reads its rows", async () => {
    state.upstream = {
      ok: true,
      data: {
        invitations: [
          {
            invitation_id: "11111111-1111-4111-8111-111111111111",
            workspace_name: "Acme",
            role: "member",
            expires_at: "2026-10-18T12:00:00+00:00",
            inviter_name: null,
          },
        ],
      },
    };
    const rows = await listMyInvitations();
    expect(asked).toEqual(["/me/invitations"]);
    expect(rows?.invitations.map((r) => r.workspaceName)).toEqual(["Acme"]);
  });

  it("is an empty list only when the API answered one", async () => {
    expect(await listMyInvitations()).toEqual({ invitations: [], canResend: false });
  });

  it.each([
    ["no session", () => (state.token = null)],
    ["a refusal", () => (state.upstream = { ok: false, status: 503, error: "unreachable" })],
    ["an answer with no list", () => (state.upstream = { ok: true, data: {} })],
  ])("is null, never empty, after %s", async (_why, arrange) => {
    arrange();
    expect(await listMyInvitations()).toBeNull();
  });
});
