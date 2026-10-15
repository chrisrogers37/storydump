/**
 * `GET /api/workspaces/[id]/connection-results/[resultId]` — what one
 * Instagram connect's callback applied (#1644). A forwarder: only an
 * id-shaped result travels, the answer comes back whole, and the API's house
 * 404 (another person, session or workspace, or an expired result) passes
 * through as it came. `targetFetch` is the real one, against a stubbed
 * `fetch`; only the session is replaced.
 */

import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const WS = "11111111-1111-4111-8111-111111111111";
const RESULT = "77777777-7777-4777-8777-777777777777";

/** `connect_choice.result`, whole. */
const APPLIED = {
  account_id: "55555555-5555-4555-8555-555555555555",
  source_id: "22222222-2222-4222-8222-222222222222",
  outcome: "connected_history_off_mix_conflict",
  history_enabled: false,
  mix_revision: 10,
  mix_reset_applied: false,
  next_action: "review_mix",
  current_mix_revision: 11,
  created_at: "2026-10-12T14:00:00+00:00",
};

const state: { token: string | null } = { token: "tok-test" };
let upstream: { status: number; body: unknown };
const sent: string[] = [];

vi.mock("@/lib/session", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/session")>()),
  getSessionToken: async () => state.token,
}));

const { GET } = await import("./route");

function ask(resultId = RESULT) {
  const request = new NextRequest(
    `https://storydump.app/api/workspaces/${WS}/connection-results/${resultId}`,
  );
  return GET(request, { params: Promise.resolve({ id: WS, resultId }) });
}

beforeEach(() => {
  sent.length = 0;
  state.token = "tok-test";
  upstream = { status: 200, body: APPLIED };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: unknown) => {
      sent.push(String(url));
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

describe("the connection result's forwarder", () => {
  it("reads the result by its id and returns it whole", async () => {
    const res = await ask();
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual(APPLIED);
    expect(sent).toEqual([expect.stringMatching(new RegExp(`/api/v1/workspaces/${WS}/connection-results/${RESULT}$`))]);
  });

  it("lets nothing but an id travel", async () => {
    for (const id of ["nope", `${RESULT}0`, "..%2Faccounts"]) {
      const res = await ask(id);
      expect(res.status, id).toBe(400);
      expect(await res.json()).toEqual({ error: "invalid_result" });
    }
    expect(sent).toHaveLength(0);
  });

  it("passes the house 404 through, and needs a session", async () => {
    upstream = { status: 404, body: { detail: "not found" } };
    const res = await ask();
    expect(res.status).toBe(404);
    expect(await res.json()).toEqual({ error: "http_404" });

    state.token = null;
    expect((await ask()).status).toBe(401);
    expect(sent).toHaveLength(1);
  });
});
