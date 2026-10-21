/**
 * `/api/workspaces/[id]/sources/[sourceId]/policy` — an Instagram past-posts
 * source's policy. GET is a member's read and
 * PUT an admin's write; both forward the API's envelope whole, and a refusal
 * keeps its reason, its status and its revision.
 *
 * Both ids are interpolated into the API's path, so each is checked to be a
 * UUID before anything is sent: the check is what stops an id from naming some
 * other route.
 *
 * `targetFetch` is the real one, run against a stubbed `fetch`, so the refusal
 * cases cover the whole seam (the API's `reason`, the revision field the client
 * lets through, `passThrough`) and not a stand-in that answers whatever the
 * test said. Only the session is replaced.
 */

import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { parseSourcePolicy } from "@/lib/source-policy";

const WS = "11111111-1111-4111-8111-111111111111";
const SRC = "22222222-2222-4222-8222-222222222222";
const OTHER_WS = "99999999-9999-4999-8999-999999999999";
const OTHER_SRC = "88888888-8888-4888-8888-888888888888";

/** What a save sends: the schema, the revision read and the three v1 fields. */
const BODY = {
  schema_version: 1,
  expected_revision: 4,
  minimum_age_days: 30,
  include_credited: false,
  include_unknown: true,
};

/**
 * The envelope GET and a successful PUT both answer,
 * including the keys the editor never reads.
 */
const ENVELOPE = {
  schema_version: 1,
  revision: 5,
  policy: {
    minimum_age_days: 30,
    include_credited: false,
    include_unknown: true,
    eligibility: "supported",
    ranking: "none",
    destination_scope: "origin_only",
  },
  effective: { minimum_age_days: 30 },
  bounds: { minimum_age_days: { min: 0, max: 36500 } },
  capabilities: { editable: ["minimum_age_days", "include_credited", "include_unknown"] },
};

type Upstream = { status: number; body: unknown } | { unreachable: true };
type Ids = { id?: string; sourceId?: string };

const state: { token: string | null } = { token: "tok-test" };
let upstream: Upstream;
const sent: Array<{ url: string; init: RequestInit }> = [];

vi.mock("@/lib/session", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/session")>()),
  getSessionToken: async () => state.token,
}));

const { GET, PUT } = await import("./route");

const urlOf = ({ id = WS, sourceId = SRC }: Ids) =>
  `https://storydump.app/api/workspaces/${id}/sources/${sourceId}/policy`;

function read(ids: Ids = {}) {
  return GET(new NextRequest(urlOf(ids)), {
    params: Promise.resolve({ id: ids.id ?? WS, sourceId: ids.sourceId ?? SRC }),
  });
}

function write(
  body: unknown,
  options: Ids & { headers?: Record<string, string>; raw?: string } = {},
) {
  const request = new NextRequest(urlOf(options), {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...options.headers },
    body: options.raw ?? JSON.stringify(body),
  });
  return PUT(request, {
    params: Promise.resolve({ id: options.id ?? WS, sourceId: options.sourceId ?? SRC }),
  });
}

beforeEach(() => {
  sent.length = 0;
  state.token = "tok-test";
  upstream = { status: 200, body: ENVELOPE };
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

/** Refusals either verb can meet, as the API sends each and as the browser must receive it. */
const COMMON_REFUSALS: Array<[string, number, Record<string, unknown>, Record<string, unknown>]> = [
  [
    "source_not_found",
    404,
    { detail: "no such source", reason: "source_not_found" },
    { error: "source_not_found" },
  ],
  [
    "unsupported_source_provider",
    400,
    { detail: "not a past-posts source", reason: "unsupported_source_provider" },
    { error: "unsupported_source_provider" },
  ],
  [
    "past_posts_not_activated",
    503,
    { detail: "past-posts source is not activated", reason: "past_posts_not_activated" },
    { error: "past_posts_not_activated" },
  ],
  [
    "a refusal that names no reason, as the tenancy gate's do",
    403,
    { detail: "insufficient role" },
    { error: "http_403" },
  ],
];

describe("GET /api/workspaces/[id]/sources/[sourceId]/policy", () => {
  it("asks the API for the source's policy as this session, and forwards the whole envelope", async () => {
    for (const ids of [{ id: WS, sourceId: SRC }, { id: OTHER_WS, sourceId: OTHER_SRC }]) {
      sent.length = 0;
      const res = await read(ids);
      expect(res.status).toBe(200);
      const answer = await res.json();
      // Nothing dropped: the revision, the effective age, the bounds and the
      // capabilities the editor draws from, and the keys it never reads.
      expect(answer).toStrictEqual(ENVELOPE);
      expect(parseSourcePolicy(answer)?.revision).toBe(5);

      expect(sent).toHaveLength(1);
      const { url, init } = sent[0];
      expect(url.endsWith(`/api/v1/workspaces/${ids.id}/sources/${ids.sourceId}/policy`)).toBe(true);
      expect(init.method ?? "GET").toBe("GET");
      expect(init.body ?? null).toBeNull();
      expect(new Headers(init.headers).get("Authorization")).toBe("Bearer tok-test");
    }
  });

  it("answers 401 without a session, and sends nothing", async () => {
    state.token = null;
    const res = await read();
    expect(res.status).toBe(401);
    expect(await res.json()).toEqual({ error: "unauthenticated" });
    expect(sent).toHaveLength(0);

    state.token = "tok-test";
    expect((await read()).status).toBe(200);
    expect(sent).toHaveLength(1);
  });

  it.each([
    ["a workspace id that is not a UUID", { id: "nope" }, "invalid_workspace"],
    ["a workspace id one character too long", { id: `${WS}0` }, "invalid_workspace"],
    ["a source id that is not a UUID", { sourceId: "nope" }, "invalid_source"],
    ["a source id one character too long", { sourceId: `${SRC}0` }, "invalid_source"],
    ["a source id that would name another route", { sourceId: "../../tokens" }, "invalid_source"],
    ["a source id that is blank", { sourceId: "" }, "invalid_source"],
  ])("answers 400 for %s, naming which, and sends nothing", async (_label, ids, error) => {
    const res = await read(ids);
    expect(res.status).toBe(400);
    expect(await res.json()).toEqual({ error });
    expect(sent).toHaveLength(0);

    expect((await read()).status).toBe(200);
    expect(sent).toHaveLength(1);
  });

  it.each(COMMON_REFUSALS)(
    "relays %s: its reason and status, and not the sentence beside them",
    async (_label, status, body, expected) => {
      upstream = { status, body };
      const res = await read();
      expect(res.status).toBe(status);
      expect(await res.json()).toStrictEqual(expected);
    },
  );

  it("asks for a refusal's revision fields as the PUT does, so a refusal that carries them is not stripped", async () => {
    // The editor re-reads after a conflict; a refusal of that read that names a
    // revision (and one that names a protocol) must reach it whole, and with
    // nothing else the API put beside it.
    upstream = {
      status: 409,
      body: {
        reason: "policy_conflict",
        current_revision: 6,
        required_protocol_version: 2,
        detail: "revision 5 is not current",
      },
    };
    const res = await read();
    expect(res.status).toBe(409);
    expect(await res.json()).toStrictEqual({
      error: "policy_conflict",
      current_revision: 6,
      required_protocol_version: 2,
    });
  });

  it("says the router is unreachable as a 503, never as empty settings", async () => {
    upstream = { unreachable: true };
    const res = await read();
    expect(res.status).toBe(503);
    expect(await res.json()).toStrictEqual({ error: "target_router_unreachable" });
  });

  it("never turns an answer it cannot read into a policy the editor would draw", async () => {
    upstream = { status: 200, body: "<html>bad gateway</html>" };
    const answer = await (await read()).json();
    expect(answer).toStrictEqual({ error: "malformed_response" });
    expect(parseSourcePolicy(answer)).toBeNull();
  });
});

describe("PUT /api/workspaces/[id]/sources/[sourceId]/policy", () => {
  it("forwards full policy and preserves conflict revision", async () => {
    upstream = {
      status: 409,
      body: { detail: "policy changed", reason: "policy_conflict", current_revision: 5 },
    };
    const res = await write(BODY);
    expect(JSON.parse(sent[0].init.body as string)).toEqual(BODY);
    expect(res.status).toBe(409);
    expect(await res.json()).toEqual({ error: "policy_conflict", current_revision: 5 });
  });

  it("PUTs the body exactly as sent, as this session, and answers the whole envelope", async () => {
    for (const ids of [{ id: WS, sourceId: SRC }, { id: OTHER_WS, sourceId: OTHER_SRC }]) {
      sent.length = 0;
      const res = await write(BODY, ids);
      expect(res.status).toBe(200);
      expect(await res.json()).toStrictEqual(ENVELOPE);

      expect(sent).toHaveLength(1);
      const { url, init } = sent[0];
      expect(url.endsWith(`/api/v1/workspaces/${ids.id}/sources/${ids.sourceId}/policy`)).toBe(true);
      expect(init.method).toBe("PUT");
      expect(init.body).toBe(JSON.stringify(BODY));
      expect(new Headers(init.headers).get("Authorization")).toBe("Bearer tok-test");
    }
  });

  it("refuses a request another site made before it looks at anything else, and lets this site's through", async () => {
    // No session and no ids either: the answer is still the cross-site one.
    state.token = null;
    const foreign: Array<Record<string, string>> = [
      { "sec-fetch-site": "cross-site" },
      { origin: "https://elsewhere.example.test" },
    ];
    for (const headers of foreign) {
      const res = await write(BODY, { headers, id: "nope", sourceId: "nope" });
      expect(res.status, JSON.stringify(headers)).toBe(403);
      expect(await res.json()).toEqual({ error: "cross_site" });
    }
    expect(sent).toHaveLength(0);

    state.token = "tok-test";
    expect((await write(BODY, { headers: { "sec-fetch-site": "same-origin" } })).status).toBe(200);
    expect(sent).toHaveLength(1);
  });

  it("answers 401 without a session, and sends nothing", async () => {
    state.token = null;
    const res = await write(BODY);
    expect(res.status).toBe(401);
    expect(await res.json()).toEqual({ error: "unauthenticated" });
    expect(sent).toHaveLength(0);
  });

  it.each([
    ["a workspace id that is not a UUID", { id: "nope" }, "invalid_workspace"],
    ["a source id that is not a UUID", { sourceId: "nope" }, "invalid_source"],
    ["a source id that would name another route", { sourceId: "../../tokens" }, "invalid_source"],
  ])("answers 400 for %s, naming which, and sends nothing", async (_label, ids, error) => {
    const res = await write(BODY, ids);
    expect(res.status).toBe(400);
    expect(await res.json()).toEqual({ error });
    expect(sent).toHaveLength(0);
  });

  it.each([
    [
      "JSON declared as text, which a plain HTML form can post",
      { raw: JSON.stringify(BODY), headers: { "Content-Type": "text/plain" } },
    ],
    ["text that is not JSON", { raw: "{minimum_age_days" }],
    ["null", { raw: "null" }],
    ["an array", { raw: JSON.stringify([BODY]) }],
    ["a string", { raw: JSON.stringify("30") }],
  ])("answers 400 malformed_body for %s, and sends nothing", async (_label, options) => {
    const res = await write(undefined, options);
    expect(res.status).toBe(400);
    expect(await res.json()).toEqual({ error: "malformed_body" });
    expect(sent).toHaveLength(0);
  });

  it.each([
    ...COMMON_REFUSALS,
    [
      "policy_conflict, with the revision that is current",
      409,
      {
        detail: "revision 4 is not current",
        reason: "policy_conflict",
        current_revision: 5,
        credential: "synthetic-secret",
      },
      { error: "policy_conflict", current_revision: 5 },
    ],
    [
      "invalid_source_policy",
      400,
      { detail: "age must be whole", reason: "invalid_source_policy" },
      { error: "invalid_source_policy" },
    ],
    [
      "unsupported_source_policy",
      400,
      { detail: "unknown schema", reason: "unsupported_source_policy" },
      { error: "unsupported_source_policy" },
    ],
  ] as Array<[string, number, Record<string, unknown>, Record<string, unknown>]>)(
    "relays %s: its reason and status, and only the revision beside them",
    async (_label, status, body, expected) => {
      upstream = { status, body };
      const res = await write(BODY);
      expect(res.status).toBe(status);
      expect(await res.json()).toStrictEqual(expected);
    },
  );

  it("carries no revision a conflict did not name as a whole number", async () => {
    for (const current_revision of ["5", 5.5, -1, null]) {
      upstream = { status: 409, body: { reason: "policy_conflict", current_revision } };
      const res = await write(BODY);
      expect(res.status).toBe(409);
      expect(await res.json(), String(current_revision)).toStrictEqual({ error: "policy_conflict" });
    }
  });
});
