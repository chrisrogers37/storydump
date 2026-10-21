/**
 * `POST /api/workspaces/[id]/category-mix/preview` — what a mix change WOULD
 * make, asked of the API and not made. The card previews
 * Reset to equal and a source's On/Off here, and makes the change with the PUT
 * beside it only on Confirm.
 *
 * The route is a forwarder. It makes four transport checks (a request this
 * site made, a session, a workspace id, a JSON object body) and has no opinion
 * about the mix, so what this pins is what must NOT change on the way through:
 * the action and its revision reach the API as the browser sent them, and the
 * answer comes back whole, or a refusal comes back with its reason, its status
 * and its revision.
 *
 * `targetFetch` is the real one, run against a stubbed `fetch`, so the refusal
 * cases cover the whole seam (the API's `reason`, the two revision fields the
 * client lets through, `passThrough`) and not a stand-in that answers whatever
 * the test said. Only the session is replaced.
 */

import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { parseSourceMix } from "@/lib/source-mix";

const WS = "11111111-1111-4111-8111-111111111111";
const OTHER_WS = "99999999-9999-4999-8999-999999999999";
const IG ="22222222-2222-4222-8222-222222222222";
const DRIVE = "33333333-3333-4333-8333-333333333333";
const DRIVE_2 = "44444444-4444-4444-8444-444444444444";
const ACCOUNT = "55555555-5555-4555-8555-555555555555";

/** A third as the API writes it: fifty digits, the residual on one of them. */
const THIRD = "0.33333333333333333333333333333333333333333333333333";
const THIRD_AND_RESIDUAL = "0.33333333333333333333333333333333333333333333333334";

const RESET = { protocol_version: 2, action: "reset_equal", expected_revision: 8 };
const TURN_ON = {
  protocol_version: 2,
  action: "set_enabled",
  source_id: IG,
  enabled: true,
  expected_revision: 8,
};

function row(over: Record<string, unknown>) {
  return {
    state: "active",
    origin_account_id: null,
    origin_handle: null,
    enabled: true,
    indexed: null,
    ...over,
  };
}

/**
 * The whole protocol-2 resource,
 * including every key this page never reads: what Instagram past posts turned on
 * would make of three sources, at the revision the preview was asked at.
 */
const PREVIEW = {
  protocol_version: 2,
  policy_version: 1,
  revision: 8,
  mode: "equal",
  supported_actions: ["reset_equal", "set_enabled", "save_custom"],
  conversion: { state: "complete", kind: "automatic_to_equal" },
  past_posts_available: true,
  reset_explanation: "enabled_membership_changed",
  rows: [
    row({
      source_id: IG,
      provider: "instagram_past_posts",
      name: "example_handle",
      origin_account_id: ACCOUNT,
      origin_handle: "example_handle",
      ratio: THIRD_AND_RESIDUAL,
      target_share: THIRD_AND_RESIDUAL,
      media_count: 700,
      indexed: 680,
    }),
    row({
      source_id: DRIVE,
      provider: "gdrive",
      name: "Event photos",
      ratio: THIRD,
      target_share: THIRD,
      media_count: 40,
    }),
    row({
      source_id: DRIVE_2,
      provider: "gdrive",
      name: "Behind the scenes",
      ratio: THIRD,
      target_share: THIRD,
      media_count: 25,
    }),
  ],
  destinations: [
    {
      account_id: ACCOUNT,
      handle: "example_handle",
      display_name: "Example",
      state: "ready",
      tone: "active",
      partial_coverage: false,
      candidates: 136,
      eligible: 3,
      leading_exclusion: { reason: "placeholder_reason", count: 4 },
      remedy: null,
      sources: [IG, DRIVE, DRIVE_2].map((source_id) => ({
        source_id,
        assignable: true,
        candidates: 40,
        eligible: 1,
        effective_share: source_id === IG ? THIRD_AND_RESIDUAL : THIRD,
        exclusions: { placeholder_reason: 4 },
        indexing: false,
        partial_coverage: false,
        stalled: false,
        needs_reconnect: false,
        oldest_observed_at: "2026-10-01T09:00:00+00:00",
      })),
    },
  ],
  guidance: { active: 1, ready: 1, intentional: null, incomplete: [] },
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

function ask(
  body: unknown,
  options: { id?: string; headers?: Record<string, string>; raw?: string } = {},
) {
  const id = options.id ?? WS;
  const request = new NextRequest(
    `https://storydump.app/api/workspaces/${id}/category-mix/preview`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json", ...options.headers },
      body: options.raw ?? JSON.stringify(body),
    },
  );
  return POST(request, { params: Promise.resolve({ id }) });
}

beforeEach(() => {
  sent.length = 0;
  state.token = "tok-test";
  upstream = { status: 200, body: PREVIEW };
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

describe("the checks the route makes itself", () => {
  it("refuses a request another site made before it looks at anything else, and lets this site's through", async () => {
    // No session and no workspace id either: the answer is still the cross-site one.
    state.token = null;
    const foreign: Array<Record<string, string>> = [
      { "sec-fetch-site": "cross-site" },
      { "sec-fetch-site": "same-site" },
      { origin: "https://elsewhere.example.test" },
    ];
    for (const headers of foreign) {
      const res = await ask(RESET, { headers, id: "nope" });
      expect(res.status, JSON.stringify(headers)).toBe(403);
      expect(await res.json()).toEqual({ error: "cross_site" });
    }
    expect(sent).toHaveLength(0);

    state.token = "tok-test";
    const own: Array<Record<string, string>> = [
      { "sec-fetch-site": "same-origin" },
      { origin: "https://storydump.app" },
      {},
    ];
    for (const headers of own) {
      const res = await ask(RESET, { headers });
      expect(res.status, JSON.stringify(headers)).toBe(200);
    }
    expect(sent).toHaveLength(3);
  });

  it("answers 401 without a session, and sends nothing", async () => {
    state.token = null;
    const res = await ask(RESET);
    expect(res.status).toBe(401);
    expect(await res.json()).toEqual({ error: "unauthenticated" });
    expect(sent).toHaveLength(0);

    state.token = "tok-test";
    expect((await ask(RESET)).status).toBe(200);
    expect(sent).toHaveLength(1);
  });

  it("answers 400 invalid_workspace for an id that is not a UUID, and sends nothing", async () => {
    for (const id of ["nope", `${WS}0`, "11111111-1111-4111-8111-11111111111g"]) {
      const res = await ask(RESET, { id });
      expect(res.status, id).toBe(400);
      expect(await res.json()).toEqual({ error: "invalid_workspace" });
    }
    expect(sent).toHaveLength(0);

    expect((await ask(RESET)).status).toBe(200);
    expect(sent).toHaveLength(1);
  });

  it.each([
    [
      "JSON declared as text, which a plain HTML form can post",
      { raw: JSON.stringify(RESET), headers: { "Content-Type": "text/plain" } },
    ],
    ["text that is not JSON", { raw: "{reset_equal" }],
    ["an empty body", { raw: "" }],
    ["null", { raw: "null" }],
    ["an array", { raw: JSON.stringify([RESET]) }],
    ["a string", { raw: JSON.stringify("reset_equal") }],
    ["a number", { raw: "8" }],
  ])("answers 400 malformed_body for %s, and sends nothing", async (_label, options) => {
    const res = await ask(undefined, options);
    expect(res.status).toBe(400);
    expect(await res.json()).toEqual({ error: "malformed_body" });
    expect(sent).toHaveLength(0);
  });

  it("forwards any JSON object, even one the API will refuse, and takes a charset on the type", async () => {
    // The checks are transport and shape only: whether the action is a real one
    // is the API's to say, by name, and answering for it here would hide it.
    for (const options of [
      { raw: "{}" },
      { raw: JSON.stringify({ action: "no_such_action" }) },
      { raw: JSON.stringify(RESET), headers: { "Content-Type": "application/json; charset=utf-8" } },
    ]) {
      const res = await ask(undefined, options);
      expect(res.status, options.raw).toBe(200);
    }
    expect(sent).toHaveLength(3);
  });
});

describe("what the API is asked", () => {
  it("is the action as the browser sent it, POSTed to the preview path of the workspace asked, as this session", async () => {
    for (const id of [WS, OTHER_WS]) {
      for (const body of [RESET, TURN_ON]) {
        sent.length = 0;
        const res = await ask(body, { id });
        expect(res.status).toBe(200);
        expect(sent).toHaveLength(1);
        const { url, init } = sent[0];
        expect(url.endsWith(`/api/v1/workspaces/${id}/category-mix/preview`)).toBe(true);
        expect(init.method).toBe("POST");
        expect(JSON.parse(init.body as string)).toStrictEqual(body);
        const headers = new Headers(init.headers);
        expect(headers.get("Authorization")).toBe("Bearer tok-test");
        expect(headers.get("Content-Type")).toBe("application/json");
      }
    }
  });

  it("touches nothing in the body: an unknown action, unknown keys and fifty-digit shares arrive exactly", async () => {
    // A route that "fixed" an action would answer for the API, and one that read
    // a share into a number would send a different share than the one it was given.
    const body = {
      protocol_version: 2,
      action: "no_such_action",
      expected_revision: 8,
      rows: [
        { source_id: IG, ratio: THIRD_AND_RESIDUAL },
        { source_id: DRIVE, ratio: THIRD },
        { source_id: DRIVE_2, ratio: THIRD },
      ],
      added_later: { nested: [1, "two", null, true] },
    };
    await ask(body);
    expect(sent).toHaveLength(1);
    expect(sent[0].init.body).toBe(JSON.stringify(body));
  });
});

describe("what comes back", () => {
  it("is the API's whole resource, unchanged, at the revision it was asked at", async () => {
    const res = await ask(TURN_ON);
    expect(res.status).toBe(200);
    const answer = await res.json();
    expect(answer).toStrictEqual(PREVIEW);
    // A preview writes nothing, so the revision is the one the action named.
    expect(answer.revision).toBe(TURN_ON.expected_revision);
    // The same resource is one the card reads, so what the route promises and
    // what the client parses cannot drift apart unseen.
    expect(parseSourceMix(answer)?.revision).toBe(8);
  });

  it.each([
    [
      "mix_conflict, with the revision that is current",
      409,
      {
        reason: "mix_conflict",
        current_revision: 9,
        detail: "revision 8 is not current",
        credential: "synthetic-secret",
      },
      { error: "mix_conflict", current_revision: 9 },
    ],
    [
      "mix_protocol_required, with the protocol it needs",
      400,
      { reason: "mix_protocol_required", required_protocol_version: 2 },
      { error: "mix_protocol_required", required_protocol_version: 2 },
    ],
    [
      "mix_membership_change_requires_action",
      400,
      { reason: "mix_membership_change_requires_action" },
      { error: "mix_membership_change_requires_action" },
    ],
    [
      "mix_conversion_unavailable",
      400,
      { reason: "mix_conversion_unavailable" },
      { error: "mix_conversion_unavailable" },
    ],
    [
      "invalid_mix_source_set, and not the sentence beside it",
      400,
      { detail: "the sources sent are not the sources in the mix", reason: "invalid_mix_source_set" },
      { error: "invalid_mix_source_set" },
    ],
    [
      "invalid_mix_sum_not_one",
      400,
      { detail: "shares do not add to one", reason: "invalid_mix_sum_not_one" },
      { error: "invalid_mix_sum_not_one" },
    ],
    [
      "a refusal that names no reason, as the tenancy gate's do",
      403,
      { detail: "insufficient role" },
      { error: "http_403" },
    ],
  ])("relays %s: its reason and status, and only the revision fields beside them", async (_label, status, body, expected) => {
    upstream = { status, body };
    const res = await ask(RESET);
    expect(res.status).toBe(status);
    expect(await res.json()).toStrictEqual(expected);
  });

  it("carries no revision a conflict did not name as a whole number", async () => {
    // The card waits for a later revision when a conflict names none; it must
    // never be handed a string or a fraction to wait for.
    for (const current_revision of ["9", 9.5, -1, null]) {
      upstream = { status: 409, body: { reason: "mix_conflict", current_revision } };
      const res = await ask(RESET);
      expect(res.status).toBe(409);
      expect(await res.json(), String(current_revision)).toStrictEqual({ error: "mix_conflict" });
    }
  });

  it("says the router is unreachable as a 503, never as an empty preview", async () => {
    upstream = { unreachable: true };
    const res = await ask(RESET);
    expect(res.status).toBe(503);
    expect(await res.json()).toStrictEqual({ error: "target_router_unreachable" });
  });

  it("never turns an answer it cannot read into a mix the card would draw", async () => {
    upstream = { status: 200, body: "<html>bad gateway</html>" };
    const res = await ask(RESET);
    const answer = await res.json();
    expect(answer).toStrictEqual({ error: "malformed_response" });
    expect(parseSourceMix(answer)).toBeNull();
  });
});
