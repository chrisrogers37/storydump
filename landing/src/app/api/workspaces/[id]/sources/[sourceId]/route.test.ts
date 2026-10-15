/**
 * `DELETE /api/workspaces/[id]/sources/[sourceId]` — remove a Drive folder from
 * the sync. A converted workspace's removal carries `protocol_version` and
 * `expected_revision` in a JSON body (`versioned_mix.remove_drive` refuses it
 * without both), and the route forwards that body unchanged and answers the
 * whole `mix` the API does. A workspace on protocol 1 sends a bare DELETE, and
 * what reaches the API is an empty object.
 *
 * `targetFetch` is the real one, run against a stubbed `fetch`; only the
 * session is replaced.
 */

import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { parseSourceMix } from "@/lib/source-mix";

const WS = "11111111-1111-4111-8111-111111111111";
const SRC = "22222222-2222-4222-8222-222222222222";
const OTHER_SRC = "88888888-8888-4888-8888-888888888888";

const PROTOCOL_2 = { protocol_version: 2, expected_revision: 8 };

/** The mix the API answers a converted workspace's removal with: the folder gone, the other source whole. */
const MIX = {
  protocol_version: 2,
  policy_version: 1,
  revision: 9,
  mode: "equal",
  supported_actions: ["reset_equal", "set_enabled", "save_custom"],
  conversion: { state: "complete", kind: "new_equal" },
  rows: [
    {
      source_id: OTHER_SRC,
      provider: "instagram_archive",
      name: "example_handle",
      state: "active",
      origin_account_id: "55555555-5555-4555-8555-555555555555",
      origin_handle: "example_handle",
      enabled: true,
      ratio: "1",
      target_share: "1",
      media_count: 700,
      indexed: 680,
    },
  ],
  destinations: [],
  guidance: { active: 0, ready: 0, intentional: null, incomplete: [] },
  archive_available: true,
  reset_explanation: null,
};

type Upstream = { status: number; body: unknown } | { unreachable: true };

const state: { token: string | null } = { token: "tok-test" };
let upstream: Upstream;
const sent: Array<{ url: string; init: RequestInit }> = [];

vi.mock("@/lib/session", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/session")>()),
  getSessionToken: async () => state.token,
}));

const { DELETE } = await import("./route");

type Options = { id?: string; sourceId?: string; headers?: Record<string, string>; raw?: string };

/** A removal: a DELETE with a JSON body when *body* is given, a bare one when it is not. */
function remove(body?: unknown, options: Options = {}) {
  const id = options.id ?? WS;
  const sourceId = options.sourceId ?? SRC;
  const hasBody = body !== undefined || options.raw !== undefined;
  return DELETE(
    new NextRequest(`https://storydump.app/api/workspaces/${id}/sources/${sourceId}`, {
      method: "DELETE",
      headers: { ...(hasBody ? { "Content-Type": "application/json" } : {}), ...options.headers },
      ...(hasBody ? { body: options.raw ?? JSON.stringify(body) } : {}),
    }),
    { params: Promise.resolve({ id, sourceId }) },
  );
}

beforeEach(() => {
  sent.length = 0;
  state.token = "tok-test";
  upstream = { status: 200, body: { source_id: SRC, state: "paused" } };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: unknown, init: unknown) => {
      sent.push({ url: String(url), init: init as RequestInit });
      if ("unreachable" in upstream) throw new TypeError("fetch failed");
      const { status, body } = upstream;
      return new Response(typeof body === "string" ? body : JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("a workspace on protocol 1", () => {
  it("sends the API an empty object for a bare DELETE, and answers the folder's id and state", async () => {
    const res = await remove();
    expect(res.status).toBe(200);
    expect(await res.json()).toStrictEqual({ source_id: SRC, sourceId: SRC, state: "paused" });

    expect(sent).toHaveLength(1);
    const { url, init } = sent[0];
    expect(url.endsWith(`/api/v1/workspaces/${WS}/sources/${SRC}`)).toBe(true);
    expect(init.method).toBe("DELETE");
    expect(init.body).toBe("{}");
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer tok-test");
  });
});

describe("a converted workspace (protocol 2)", () => {
  it("forwards the protocol and the revision the removal was made against, unchanged", async () => {
    upstream = { status: 200, body: { source_id: SRC, state: "paused", mix: MIX } };
    const res = await remove(PROTOCOL_2);
    expect(res.status).toBe(200);
    expect(sent).toHaveLength(1);
    expect(sent[0].init.method).toBe("DELETE");
    expect(JSON.parse(sent[0].init.body as string)).toStrictEqual(PROTOCOL_2);
  });

  it("answers the whole mix the API did, so the page can say what the removal left", async () => {
    upstream = { status: 200, body: { source_id: SRC, state: "paused", mix: MIX } };
    const answer = await (await remove(PROTOCOL_2)).json();
    expect(answer.mix).toStrictEqual(MIX);
    expect(answer.sourceId).toBe(SRC);
    expect(answer.state).toBe("paused");
    expect(parseSourceMix(answer.mix)?.revision).toBe(9);
  });

  it("relays a conflict with the revision that is current, and nothing else the API put beside it", async () => {
    upstream = {
      status: 409,
      body: { reason: "mix_conflict", current_revision: 9, detail: "revision 8 is not current" },
    };
    const res = await remove(PROTOCOL_2);
    expect(res.status).toBe(409);
    expect(await res.json()).toStrictEqual({ error: "mix_conflict", current_revision: 9 });
  });

  it("relays a page that is older than the mix with the protocol the API requires", async () => {
    upstream = {
      status: 400,
      body: { reason: "mix_protocol_required", required_protocol_version: 2 },
    };
    const res = await remove();
    expect(res.status).toBe(400);
    expect(await res.json()).toStrictEqual({
      error: "mix_protocol_required",
      required_protocol_version: 2,
    });
  });
});

describe("what it refuses itself, before the API is asked", () => {
  it("answers 400 invalid_source for an id that would name another route, and sends nothing", async () => {
    for (const sourceId of ["nope", "../../tokens", `${SRC}0`]) {
      const res = await remove(PROTOCOL_2, { sourceId });
      expect(res.status, sourceId).toBe(400);
      expect(await res.json()).toEqual({ error: "invalid_source" });
    }
    expect(sent).toHaveLength(0);
    expect((await remove(PROTOCOL_2)).status).toBe(200);
    expect(sent).toHaveLength(1);
  });

  it("answers 400 malformed_body for a body declared as JSON that is not, and sends nothing", async () => {
    const res = await remove(undefined, { raw: "{protocol_version" });
    expect(res.status).toBe(400);
    expect(await res.json()).toEqual({ error: "malformed_body" });
    expect(sent).toHaveLength(0);
  });

  it("refuses a request another site made, and lets this site's through", async () => {
    const res = await remove(PROTOCOL_2, { headers: { "sec-fetch-site": "cross-site" } });
    expect(res.status).toBe(403);
    expect(await res.json()).toEqual({ error: "cross_site" });
    expect(sent).toHaveLength(0);

    expect((await remove(PROTOCOL_2, { headers: { "sec-fetch-site": "same-origin" } })).status).toBe(200);
    expect(sent).toHaveLength(1);
  });

  it("answers 401 without a session", async () => {
    state.token = null;
    const res = await remove(PROTOCOL_2);
    expect(res.status).toBe(401);
    expect(await res.json()).toEqual({ error: "unauthenticated" });
    expect(sent).toHaveLength(0);
  });
});
