/**
 * `POST /api/admissions` composed whole (#1740): the page's client
 * (`postAdmission`, through `callBff`), this route, the server's client
 * (`targetFetch`) and the line the owner's page shows (`admissionLine`), each
 * the real one. `route.test.ts` replaces `targetFetch`, so it cannot see what
 * the two clients make of an answer between them; this file can.
 *
 * Only the session is stubbed. `fetch` is a bridge: the page's
 * `/api/admissions` becomes a `NextRequest` handed to the real `POST`, the
 * API's `/api/v1/admissions` gets the answer a test scripts, and any other URL
 * is recorded and refused. Every test asserts there was none, and that the
 * route did not throw: both clients turn a throw from `fetch` into an answer
 * that never came, so a bridge mistake would otherwise read as the very
 * uncertainty under test.
 *
 * Pinned: a 2xx this tier cannot read, or one that names no outcome it knows,
 * is the route's 502 `malformed_response` and the page's LOST_ANSWER — never
 * "Done", never a throw — after exactly one call to the API. A named outcome
 * and a named refusal keep their status and reason, and the page shows the
 * line the domain gives that answer; its words are pinned once, in
 * `ops-waitlist.test.ts`, so a copy edit is that file's alone. Should a 200
 * the page cannot read reach it anyway, the page's own check says the same.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

const session: { token: string | null } = { token: "tok-chain" };

vi.mock("@/lib/session", async (actual) => ({
  ...(await actual<typeof import("@/lib/session")>()),
  getSessionToken: async () => session.token,
}));

const { POST } = await import("./route");
const { TARGET_API_URL } = await import("@/lib/target-api");
const { ADMIT_OUTCOMES, admissionLine, LOST_ANSWER, postAdmission } = await import(
  "@/lib/ops-waitlist"
);

const EMAIL = "person@example.com";
/** Where `targetFetch` sends an admission. */
const API_ADMISSIONS = `${TARGET_API_URL}/api/v1/admissions`;

/** What one admission did at each hop. */
const hops: {
  /** Each call that reached the API: its method, credential and body. */
  api: Array<{ method: string | undefined; authorization: string | null; body: unknown }>;
  /** Each answer of the route, or what it threw. */
  route: Array<{ status: number; body: unknown } | { threw: string }>;
  /** Every URL the bridge does not know. */
  unexpected: string[];
} = { api: [], route: [], unexpected: [] };

/** The API's next answer. A function, since a body can be read once. */
let api: () => Response;
/** When set, what the page's own request gets in place of the route's answer. */
let page: (() => Response) | null = null;

async function bridge(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
  if (url === "/api/admissions") {
    if (page) return page();
    const headers = new Headers(init?.headers);
    // The browser's own verdict on a page's fetch, which the page cannot set.
    headers.set("sec-fetch-site", "same-origin");
    const request = new NextRequest(`https://storydump.app${url}`, {
      method: init?.method,
      headers,
      body: init?.body,
    });
    try {
      const response = await POST(request);
      hops.route.push({ status: response.status, body: await response.clone().json() });
      return response;
    } catch (err) {
      // What Next makes of a route that throws: a 500 the route never wrote.
      hops.route.push({ threw: String(err) });
      return new Response(null, { status: 500 });
    }
  }
  if (url === API_ADMISSIONS) {
    hops.api.push({
      method: init?.method,
      authorization: new Headers(init?.headers).get("authorization"),
      body: JSON.parse(String(init?.body)),
    });
    return api();
  }
  hops.unexpected.push(url);
  throw new TypeError(`the bridge refuses ${url}`);
}

/** An answer of *status* carrying *text* as it comes, JSON unless said otherwise. */
const answer =
  (text: string | null, status = 200, type = "application/json") =>
  () =>
    new Response(text, { status, headers: { "content-type": type } });

/** One admission from the owner's page: the client's result, and the line it leaves. */
async function admit(resend = false) {
  const result = await postAdmission(EMAIL, resend);
  return { result, line: admissionLine(result) };
}

beforeEach(() => {
  hops.api.length = 0;
  hops.route.length = 0;
  hops.unexpected.length = 0;
  session.token = "tok-chain";
  page = null;
  api = answer(JSON.stringify({ outcome: "admitted" }));
  vi.stubGlobal("fetch", vi.fn(bridge));
});

afterEach(() => {
  vi.unstubAllGlobals();
  // Every test, written once: a bridge mistake would otherwise read as the
  // very uncertainty under test.
  expect(hops.unexpected).toEqual([]);
});

/** The route's answer when the API's could not be read as an outcome. */
const UNCONFIRMED = { status: 502, body: { error: "malformed_response" } };

/** API answers of 2xx that name no outcome this tier knows. */
const UNREADABLE_FROM_THE_API: Array<[string, () => Response]> = [
  ["is not JSON at all", answer("<html><body>Bad gateway</body></html>", 200, "text/html")],
  ["is JSON cut short", answer('{"outcome":"admi')],
  ["is empty", answer("")],
  ["is a 202 that is not JSON", answer("accepted", 202, "text/plain")],
  ["is no content (204)", () => new Response(null, { status: 204 })],
  ["is null", answer("null")],
  ["is an empty object", answer("{}")],
  ["names no outcome", answer('{"job_id":"j-1"}')],
  ["names an empty outcome", answer('{"outcome":""}')],
  ["names a null outcome", answer('{"outcome":null}')],
  ["names a number", answer('{"outcome":1}')],
  ["names a list holding a known outcome", answer('{"outcome":["admitted"]}')],
  ["names an object", answer('{"outcome":{"admitted":true}}')],
  ["names an outcome it does not know", answer('{"outcome":"approved"}')],
  ["names a known outcome in capitals", answer('{"outcome":"Admitted"}')],
  ["names an object's own method, toString", answer('{"outcome":"toString"}')],
  ["names constructor", answer('{"outcome":"constructor"}')],
  ["is a list", answer("[]")],
  ["is a list holding an answer", answer('[{"outcome":"admitted"}]')],
  ["is a bare string naming an outcome", answer('"admitted"')],
  ["is a number", answer("7")],
  ["is true", answer("true")],
  ["is false", answer("false")],
];

/** Route answers of 200 that the page cannot read as an outcome. */
const UNREADABLE_FROM_THE_ROUTE: Array<[string, () => Response]> = [
  ["is not JSON at all", answer("<html><body>Bad gateway</body></html>", 200, "text/html")],
  ["is no content (204)", () => new Response(null, { status: 204 })],
  ["is null", answer("null")],
  ["is an empty object", answer("{}")],
  ["names a null outcome", answer('{"outcome":null}')],
  ["carries a refusal's reason", answer('{"error":"malformed_response"}')],
  ["names an outcome it does not know", answer('{"outcome":"approved"}')],
  ["names toString", answer('{"outcome":"toString"}')],
  ["is a list holding an answer", answer('[{"outcome":"admitted"}]')],
  ["is a bare string naming an outcome", answer('"admitted"')],
];

describe("an answer of 2xx that names no outcome this tier knows", () => {
  it.each(UNREADABLE_FROM_THE_API)(
    "is the route's 502 and the page's lost answer when it %s",
    async (_why, upstream) => {
      api = upstream;
      const { result, line } = await admit();
      expect(hops.api).toHaveLength(1);
      expect(hops.route).toEqual([UNCONFIRMED]);
      expect(result).toEqual({
        ok: false,
        status: 502,
        error: "malformed_response",
        body: UNCONFIRMED.body,
      });
      expect(line).toEqual({ text: LOST_ANSWER, ok: false });
    },
  );
});

describe("an answer the API names", () => {
  it("carries the page's two fields to the API once, with the session as the credential", async () => {
    await admit(true);
    expect(hops.api).toEqual([
      { method: "POST", authorization: "Bearer tok-chain", body: { email: EMAIL, resend: true } },
    ]);
  });

  /** Whether each outcome, named in a 200, went as asked: written out, never derived. */
  const WENT_AS_ASKED = {
    admitted: true,
    resent: true,
    admitted_before: true,
    signed_up: false,
    invalid_email: false,
    not_configured: false,
  } as const;

  it("decides every outcome the vocabulary has", () => {
    expect(Object.keys(WENT_AS_ASKED).sort()).toEqual([...ADMIT_OUTCOMES].sort());
  });

  it.each(Object.entries(WENT_AS_ASKED))(
    "relays %s named in a 200 at every hop, and the page decides it (%s)",
    async (outcome, ok) => {
      api = answer(JSON.stringify({ outcome, job_id: "j-1" }));
      const { result, line } = await admit(outcome === "resent");
      expect(hops.api).toHaveLength(1);
      expect(hops.route).toEqual([{ status: 200, body: { outcome } }]);
      expect(result).toEqual({ ok: true, status: 200, data: { outcome } });
      expect(line.ok).toBe(ok);
      expect(line.text).not.toBe(LOST_ANSWER);
      expect(line).toEqual(admissionLine({ ok: true, status: 200, data: { outcome } }));
    },
  );

  it.each([
    [409, { detail: "that address has already signed up", reason: "signed_up" }, "signed_up"],
    [
      400,
      { detail: "that is not an address an admission can hold", reason: "invalid_email" },
      "invalid_email",
    ],
    [403, { detail: "this route is for the people in OPS_USER_IDS", reason: "not_ops" }, "not_ops"],
    [
      503,
      { detail: "the email's claim link cannot be built: set WEB_APP_URL", reason: "not_configured" },
      "not_configured",
    ],
  ])("keeps the API's %s refusal, its reason, status and own words", async (status, body, error) => {
    api = answer(JSON.stringify(body), status);
    const { result, line } = await admit();
    expect(hops.api).toHaveLength(1);
    expect(hops.route).toEqual([{ status, body: { error } }]);
    expect(result).toEqual({ ok: false, status, error, body: { error } });
    expect(line.ok).toBe(false);
    expect(line.text).not.toBe(LOST_ANSWER);
    expect(line).toEqual(admissionLine({ ok: false, status, error, body: { error } }));
  });

  it("keeps the API's 401 a session refusal, and says nothing changed", async () => {
    api = answer(JSON.stringify({ detail: "authentication required" }), 401);
    const { result, line } = await admit();
    expect(hops.route).toEqual([{ status: 401, body: { error: "http_401" } }]);
    expect(result).toMatchObject({ ok: false, status: 401, error: "http_401" });
    expect(line.ok).toBe(false);
    expect(line.text).toContain("You are not signed in");
    expect(line.text).toContain("Nothing changed.");
  });

  it("answers 401 without a session, and the API is never asked", async () => {
    session.token = null;
    const { result, line } = await admit();
    expect(hops.api).toEqual([]);
    expect(hops.route).toEqual([{ status: 401, body: { error: "unauthenticated" } }]);
    expect(result).toMatchObject({ ok: false, status: 401, error: "unauthenticated" });
    expect(line.ok).toBe(false);
    expect(line.text).toContain("Nothing changed.");
  });

  it("says an API it cannot reach may have taken it", async () => {
    api = () => {
      throw new TypeError("fetch failed");
    };
    const { result, line } = await admit();
    expect(hops.api).toHaveLength(1);
    expect(hops.route).toEqual([{ status: 503, body: { error: "target_router_unreachable" } }]);
    expect(result).toMatchObject({ ok: false, status: 503 });
    expect(line).toEqual({ text: LOST_ANSWER, ok: false });
  });
});

describe("a 200 from the route that the page cannot read", () => {
  // The page's own check, apart from the route's 502: whatever the route
  // answers, a 200 that names no outcome is not a yes.
  it.each(UNREADABLE_FROM_THE_ROUTE)("is the lost answer when it %s", async (_why, answered) => {
    page = answered;
    const { result, line } = await admit();
    expect(hops.api).toEqual([]);
    expect(hops.route).toEqual([]);
    expect(result.ok).toBe(true);
    expect(line).toEqual({ text: LOST_ANSWER, ok: false });
  });
});
