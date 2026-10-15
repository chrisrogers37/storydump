/**
 * `POST /api/workspaces/[id]/sources` — pick a Drive folder under the
 * workspace's grant. The route forwards its body to the API unchanged, and that
 * is the whole of what a converted workspace needs from it: `protocol_version`
 * and `expected_revision` ride through (`versioned_mix.connect_drive` refuses a
 * converted workspace without both), the whole `mix` the API answers rides back,
 * and a conflict's revision and a stale page's required protocol survive a
 * refusal. A workspace still on protocol 1 sends neither field, and the body
 * that reaches the API is the folder alone.
 *
 * `targetFetch` is the real one, run against a stubbed `fetch`, so the
 * refusal cases cover the whole seam; only the session is replaced.
 */

import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { parseSourceMix } from "@/lib/source-mix";

const WS = "11111111-1111-4111-8111-111111111111";
const SRC = "22222222-2222-4222-8222-222222222222";
const ARCHIVE = "33333333-3333-4333-8333-333333333333";

/** What the picker sends from a workspace on protocol 1: the folder alone. */
const PROTOCOL_1 = { folder_ref: "f1", folder_name: "Trips" };

/** What it sends from a converted workspace: the same, against the revision it read. */
const PROTOCOL_2 = { ...PROTOCOL_1, protocol_version: 2, expected_revision: 8 };

/** The mix resource the API answers a converted workspace's pick with, every key of it. */
const MIX = {
  protocol_version: 2,
  policy_version: 1,
  revision: 9,
  mode: "equal",
  supported_actions: ["reset_equal", "set_enabled", "save_custom"],
  conversion: { state: "complete", kind: "new_equal" },
  rows: [
    {
      source_id: ARCHIVE,
      provider: "instagram_archive",
      name: "example_handle",
      state: "active",
      origin_account_id: "55555555-5555-4555-8555-555555555555",
      origin_handle: "example_handle",
      enabled: true,
      ratio: "0.5",
      target_share: "0.5",
      media_count: 700,
      indexed: 680,
    },
    {
      source_id: SRC,
      provider: "gdrive",
      name: "Trips",
      state: "active",
      origin_account_id: null,
      origin_handle: null,
      enabled: true,
      ratio: "0.5",
      target_share: "0.5",
      media_count: 0,
      indexed: null,
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

const { POST } = await import("./route");

function pick(body: unknown, options: { id?: string; headers?: Record<string, string>; raw?: string } = {}) {
  const id = options.id ?? WS;
  return POST(
    new NextRequest(`https://storydump.app/api/workspaces/${id}/sources`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...options.headers },
      body: options.raw ?? JSON.stringify(body),
    }),
    { params: Promise.resolve({ id }) },
  );
}

beforeEach(() => {
  sent.length = 0;
  state.token = "tok-test";
  upstream = { status: 201, body: { source_id: SRC, created: true } };
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

/** The JSON the API was sent. */
const sentBody = () => JSON.parse(sent[0].init.body as string);

describe("a workspace on protocol 1", () => {
  it("sends the API the folder and its name and nothing else, and answers the created source's id", async () => {
    const res = await pick(PROTOCOL_1);
    expect(res.status).toBe(200);
    expect(await res.json()).toStrictEqual({ source_id: SRC, sourceId: SRC, created: true });

    expect(sent).toHaveLength(1);
    const { url, init } = sent[0];
    expect(url.endsWith(`/api/v1/workspaces/${WS}/sources`)).toBe(true);
    expect(init.method).toBe("POST");
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer tok-test");
    // Not one key the picker did not send: the API refuses protocol 2's fields
    // on a workspace that has not converted.
    expect(sentBody()).toStrictEqual(PROTOCOL_1);
  });
});

describe("a converted workspace (protocol 2)", () => {
  it("forwards the protocol and the revision the pick was made against, with the folder, unchanged", async () => {
    upstream = { status: 201, body: { source_id: SRC, created: true, mix: MIX } };
    const res = await pick(PROTOCOL_2);
    expect(res.status).toBe(200);
    expect(sentBody()).toStrictEqual(PROTOCOL_2);
  });

  it("answers the whole mix the API did, so the page can say what the pick left", async () => {
    upstream = { status: 201, body: { source_id: SRC, created: true, mix: MIX } };
    const answer = await (await pick(PROTOCOL_2)).json();
    // Every key of it, the revision and the shares the page reads included.
    expect(answer.mix).toStrictEqual(MIX);
    expect(answer.sourceId).toBe(SRC);
    expect(answer.created).toBe(true);
    const read = parseSourceMix(answer.mix);
    expect(read?.revision).toBe(9);
    expect(read?.rows.map((row) => row.targetShare)).toEqual(["0.5", "0.5"]);
  });

  it("relays a conflict with the revision that is current, and nothing else the API put beside it", async () => {
    upstream = {
      status: 409,
      body: { reason: "mix_conflict", current_revision: 9, detail: "revision 8 is not current" },
    };
    const res = await pick(PROTOCOL_2);
    expect(res.status).toBe(409);
    expect(await res.json()).toStrictEqual({ error: "mix_conflict", current_revision: 9 });
  });

  it("relays a page that is older than the mix with the protocol the API requires", async () => {
    upstream = {
      status: 400,
      body: { reason: "mix_protocol_required", required_protocol_version: 2 },
    };
    const res = await pick(PROTOCOL_1);
    expect(res.status).toBe(400);
    expect(await res.json()).toStrictEqual({
      error: "mix_protocol_required",
      required_protocol_version: 2,
    });
  });
});

describe("what it refuses itself, before the API is asked", () => {
  it("answers 400 folder_required for a body with no folder, version fields or not", async () => {
    for (const body of [{}, { protocol_version: 2, expected_revision: 8 }, { folder_ref: "  " }]) {
      const res = await pick(body);
      expect(res.status).toBe(400);
      expect(await res.json()).toEqual({ error: "folder_required" });
    }
    expect(sent).toHaveLength(0);

    // A folder, with the same fields, goes through.
    expect((await pick(PROTOCOL_2)).status).toBe(200);
    expect(sent).toHaveLength(1);
  });

  it("answers 400 malformed_body for a body that is not JSON, and sends nothing", async () => {
    const res = await pick(undefined, { raw: "{folder_ref" });
    expect(res.status).toBe(400);
    expect(await res.json()).toEqual({ error: "malformed_body" });
    expect(sent).toHaveLength(0);
  });

  it("refuses a request another site made, and lets this site's through", async () => {
    const res = await pick(PROTOCOL_2, { headers: { "sec-fetch-site": "cross-site" } });
    expect(res.status).toBe(403);
    expect(await res.json()).toEqual({ error: "cross_site" });
    expect(sent).toHaveLength(0);

    expect((await pick(PROTOCOL_2, { headers: { "sec-fetch-site": "same-origin" } })).status).toBe(200);
    expect(sent).toHaveLength(1);
  });

  it("answers 401 without a session, and 502 for an answer with no source", async () => {
    state.token = null;
    expect((await pick(PROTOCOL_2)).status).toBe(401);
    expect(sent).toHaveLength(0);

    state.token = "tok-test";
    upstream = { status: 201, body: { created: true } };
    const res = await pick(PROTOCOL_2);
    expect(res.status).toBe(502);
    expect(await res.json()).toEqual({ error: "malformed_response" });
  });
});
