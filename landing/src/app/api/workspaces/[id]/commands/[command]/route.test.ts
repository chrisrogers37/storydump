/**
 * `/api/workspaces/[id]/commands/[command]` — the one command door, and the
 * one route that asks for a refusal's facts (#1413 phase 6).
 *
 * The allow-list itself is pinned in `refusal-facts.test.ts` and the switch in
 * `target-api.test.ts`; this pins the route's part: it asks for facts on every
 * command it forwards and relays them beside the reason, and a refusal with
 * none answers exactly as it did before.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

const WS = "11111111-1111-4111-8111-111111111111";
const INTENT = "33333333-3333-4333-8333-333333333333";

const state: { upstream: unknown } = { upstream: { ok: true, data: {} } };
const captured: Array<{ path: string; init?: Record<string, unknown> }> = [];

vi.mock("@/lib/session", () => {
  const UUID =
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  return {
    getSessionToken: async () => "tok-test",
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

function post(command: string, body: unknown) {
  const request = new Request(
    `https://storydump.app/api/workspaces/${WS}/commands/${command}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  ) as unknown as Parameters<typeof POST>[0];
  return POST(request, { params: Promise.resolve({ id: WS, command }) });
}

beforeEach(() => {
  captured.length = 0;
  state.upstream = { ok: true, data: {} };
});

describe("a refusal's facts", () => {
  // The route forwards any offered command alike; which ones the port sends
  // facts for is the port's business, so one command stands for all here.
  it("are asked for on the command it forwards", async () => {
    await post("approve", { intent_id: INTENT });
    expect(captured).toHaveLength(1);
    expect(captured[0].path).toBe(`/workspaces/${WS}/commands/approve`);
    expect(captured[0].init?.refusalFacts).toBe(true);
  });

  it("ride back to the browser beside the reason, with the port's status", async () => {
    state.upstream = {
      ok: false,
      status: 409,
      error: "locked",
      facts: { overridable: true, in_the_way: ["recent"] },
    };
    const res = await post("approve", { intent_id: INTENT });
    expect(res.status).toBe(409);
    expect(await res.json()).toEqual({
      error: "locked",
      facts: { overridable: true, in_the_way: ["recent"] },
    });
  });

  it("leave a refusal without them exactly as it was", async () => {
    state.upstream = { ok: false, status: 409, error: "illegal_transition" };
    const res = await post("approve", { intent_id: INTENT });
    expect(res.status).toBe(409);
    expect(await res.json()).toStrictEqual({ error: "illegal_transition" });
  });

  it("are not asked for when the route refuses before forwarding", async () => {
    const res = await post("approve", { intent_id: "not-an-id" });
    expect(res.status).toBe(400);
    expect(captured).toHaveLength(0);
  });
});
