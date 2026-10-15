import { readFileSync } from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { NextRequest, NextResponse } from "next/server";
import { describe, expect, it } from "vitest";
import { passThrough, readJsonBody, refuseCrossSite } from "./route-guards";

/**
 * THE WORDS ARE THE CONTRACT. Every sentence the browser shows for a refusal
 * is chosen by a `switch` on one of these strings; a route that answers a
 * synonym falls through to "That did not work", which names no remedy. This
 * file exists so that a rename shows up as a failing test rather than as a
 * vaguer banner nobody files a bug about.
 */
describe("passThrough relays the API's own refusal", () => {
  it("keeps the reason and the status exactly", async () => {
    const res = passThrough({ error: "illegal_transition", status: 409 });
    expect(res.status).toBe(409);
    expect(await res.json()).toEqual({ error: "illegal_transition" });
  });

  it("does not re-code a 4xx the API already named", async () => {
    // The whole point: this tier has no opinion about the API's reason. A
    // guard that "normalised" one is how two tiers come to disagree about what
    // happened to a write.
    const res = passThrough({ error: "not_connected", status: 422 });
    expect(res.status).toBe(422);
    expect(await res.json()).toEqual({ error: "not_connected" });
  });

  it("relays a refusal's facts beside its reason when the result carries them", async () => {
    // Only a command refusal can carry facts, already through the allow-list
    // (`refusal-facts.ts`); this relays them as it relays the reason.
    const res = passThrough({
      error: "locked",
      status: 409,
      facts: { overridable: true, in_the_way: ["recent"] },
    });
    expect(res.status).toBe(409);
    expect(await res.json()).toEqual({
      error: "locked",
      facts: { overridable: true, in_the_way: ["recent"] },
    });
  });
});

const GUARDS = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "./route-guards.ts",
);

describe("the guard vocabulary", () => {
  it("spells the three refusals the browser switches on", () => {
    // AN UNREADABLE SOURCE IS A FAILURE, NEVER A SKIP — the
    // `intent-states-contract` rule.
    let src: string;
    try {
      src = readFileSync(GUARDS, "utf8");
    } catch (err) {
      throw new Error(`cannot read ${GUARDS}: ${err}`);
    }
    expect(src).toContain('{ error: "unauthenticated" }, { status: 401 }');
    expect(src).toContain('{ error: "invalid_workspace" }, { status: 400 }');
    expect(src).toContain('{ error: "malformed_body" }, { status: 400 }');
  });
});

const THIS_ORIGIN = "https://app.example.test";

function post(
  headers: Record<string, string>,
  body: BodyInit = "{}",
): NextRequest {
  return new NextRequest(`${THIS_ORIGIN}/api/workspaces`, {
    method: "POST",
    headers,
    body,
  });
}

/**
 * The browser says where a request came from, and a cross-site one is refused
 * before anything else runs. `SameSite=Lax` keeps the session cookie off a
 * cross-SITE post, but every subdomain of the product domain is the same site,
 * so the cookie alone cannot tell a sibling host from this one.
 */
describe("refuseCrossSite", () => {
  it.each<[string, Record<string, string>]>([
    ["a cross-site request", { "sec-fetch-site": "cross-site" }],
    ["a same-site request from another subdomain", { "sec-fetch-site": "same-site" }],
    ["a request no page made", { "sec-fetch-site": "none" }],
    [
      "the browser's verdict even when Origin names this origin",
      { "sec-fetch-site": "cross-site", origin: THIS_ORIGIN },
    ],
    ["a foreign Origin with no Sec-Fetch-Site", { origin: "https://elsewhere.example.test" }],
    ["an opaque Origin", { origin: "null" }],
    ["an origin that only starts with this one", { origin: `${THIS_ORIGIN}.elsewhere.example` }],
    ["this host over plain http", { origin: "http://app.example.test" }],
    ["this host on another port", { origin: `${THIS_ORIGIN}:8443` }],
  ])("refuses %s", async (_label, headers) => {
    const refused = refuseCrossSite(post(headers));
    expect(refused?.status).toBe(403);
    expect(await refused?.json()).toEqual({ error: "cross_site" });
  });

  it.each<[string, Record<string, string>]>([
    ["a same-origin request", { "sec-fetch-site": "same-origin" }],
    ["a same-origin request that also sends Origin", { "sec-fetch-site": "same-origin", origin: THIS_ORIGIN }],
    ["this origin with no Sec-Fetch-Site", { origin: THIS_ORIGIN }],
    // Not a browser, so it carries no victim's cookie and is not a CSRF vector.
    ["a request with neither header", {}],
  ])("lets through %s", (_label, headers) => {
    expect(refuseCrossSite(post(headers))).toBeNull();
  });
});

/**
 * A plain HTML form can post `text/plain`, `application/x-www-form-urlencoded`
 * or `multipart/form-data` with no CORS preflight, and a `text/plain` body can
 * be written to parse as JSON. Every caller on this tier sends
 * `application/json`, so that is the only type read.
 */
describe("readJsonBody", () => {
  it.each([
    "text/plain",
    "text/plain;charset=UTF-8",
    "application/x-www-form-urlencoded",
    "multipart/form-data; boundary=x",
  ])("refuses a %s body even when it parses as JSON", async (contentType) => {
    const read = await readJsonBody(post({ "content-type": contentType }, '{"name":"x"}'));
    expect(read).toBeInstanceOf(NextResponse);
    expect((read as NextResponse).status).toBe(400);
    expect(await (read as NextResponse).json()).toEqual({ error: "malformed_body" });
  });

  it("refuses a body that declares no type", async () => {
    // Bytes rather than a string: a string body is given `text/plain` by default.
    const read = await readJsonBody(post({}, new TextEncoder().encode('{"name":"x"}')));
    expect(read).toBeInstanceOf(NextResponse);
    expect(await (read as NextResponse).json()).toEqual({ error: "malformed_body" });
  });

  it("reads application/json with a parameter, in any case", async () => {
    const read = await readJsonBody(
      post({ "content-type": "Application/JSON; charset=utf-8" }, '{"name":"x"}'),
    );
    expect(read).toEqual({ raw: { name: "x" } });
  });

  it("still refuses a JSON-typed body that is not JSON", async () => {
    const read = await readJsonBody(post({ "content-type": "application/json" }, "{not json"));
    expect(read).toBeInstanceOf(NextResponse);
    expect(await (read as NextResponse).json()).toEqual({ error: "malformed_body" });
  });
});
