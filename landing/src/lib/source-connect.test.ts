/**
 * The Drive connect leg's browser half (#1065).
 *
 * The weight here is on `isAuthorizationUrl`, because it guards a value the
 * app NAVIGATES TO. Everything else in this file is ordinary request plumbing;
 * that function is the one place where being approximately right is a
 * vulnerability rather than a bug.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  connectRefusalCopy,
  isAuthorizationUrl,
  requestSourceConnect,
} from "./source-connect";

const WS = "11111111-1111-4111-8111-111111111111";
const SRC = "44444444-4444-4444-8444-444444444444";
const REAL =
  "https://accounts.google.com/o/oauth2/v2/auth?response_type=code&client_id=x&state=y";

let captured: { url: string; init: RequestInit }[];

function stubFetch(body: unknown, status = 200) {
  captured = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit) => {
      captured.push({ url, init });
      return {
        ok: status >= 200 && status < 300,
        status,
        json: async () => body,
      } as unknown as Response;
    }),
  );
}

beforeEach(() => vi.unstubAllGlobals());

describe("isAuthorizationUrl — the redirect guard", () => {
  it("accepts the URL the API actually builds", () => {
    expect(isAuthorizationUrl(REAL)).toBe(true);
  });

  // The trap this function exists for. A suffix test passes BOTH of these,
  // and both are registrable by someone who is not Google.
  it.each([
    "https://evil-accounts.google.com/o/oauth2/v2/auth",
    "https://accounts.google.com.evil.example/o/oauth2/v2/auth",
    "https://notaccounts.google.com/",
  ])("refuses the look-alike host %s", (url) => {
    expect(isAuthorizationUrl(url)).toBe(false);
  });

  it("refuses a non-default port on the real host", () => {
    // Not a threat we could be exposed to — reaching it means holding
    // Google's own host — but `host` equality refuses it for free, and this
    // pins that choice so a later switch to `hostname` is a visible change
    // rather than a silent loosening.
    expect(isAuthorizationUrl("https://accounts.google.com:1234/o/oauth2/v2/auth")).toBe(
      false,
    );
  });

  it("accepts the real host with its DEFAULT port written out", () => {
    // The other side of that choice, so the strictness is bounded rather than
    // assumed: `host` omits :443 for https, so this must still pass.
    expect(isAuthorizationUrl("https://accounts.google.com:443/o/oauth2/v2/auth")).toBe(
      true,
    );
  });

  it.each([
    "http://accounts.google.com/o/oauth2/v2/auth",
    "//accounts.google.com/o/oauth2/v2/auth",
    "/o/oauth2/v2/auth",
    "javascript:alert(1)",
    "",
    "not a url",
  ])("refuses %s", (url) => {
    expect(isAuthorizationUrl(url)).toBe(false);
  });
});

describe("requestSourceConnect", () => {
  it("posts to the per-source route, with the source id in the path", async () => {
    stubFetch({ authorizationUrl: REAL });
    const result = await requestSourceConnect(WS, SRC);

    expect(captured[0].url).toBe(`/api/workspaces/${WS}/sources/${SRC}/connect`);
    expect(captured[0].init.method).toBe("POST");
    expect(result).toEqual({ ok: true, authorizationUrl: REAL });
  });

  it("sends NO idempotency key — minting a state is last-issued-wins", async () => {
    stubFetch({ authorizationUrl: REAL });
    await requestSourceConnect(WS, SRC);
    const headers = (captured[0].init.headers ?? {}) as Record<string, string>;
    expect(Object.keys(headers).map((k) => k.toLowerCase())).not.toContain(
      "idempotency-key",
    );
  });

  it("refuses a 200 whose URL would not pass the guard", async () => {
    // The client re-checks what the route already checked, because this is the
    // line before the navigation. A 200 with a bad URL is a failure, not a
    // success with a missing field.
    stubFetch({ authorizationUrl: "https://evil.example/steal" });
    const result = await requestSourceConnect(WS, SRC);
    expect(result.ok).toBe(false);
    expect(result.ok === false && result.error).toBe("malformed_authorization_url");
  });

  it("refuses a 200 with no URL at all", async () => {
    stubFetch({});
    const result = await requestSourceConnect(WS, SRC);
    expect(result.ok).toBe(false);
  });

  it("surfaces the API's reason on a refusal", async () => {
    stubFetch({ error: "not found" }, 404);
    const result = await requestSourceConnect(WS, SRC);
    expect(result.ok).toBe(false);
    expect(result.ok === false && result.status).toBe(404);
    expect(connectRefusalCopy("not found")).toMatch(/no longer here/i);
  });

  it("reports an unreachable app without claiming anything happened", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("network"); }));
    const result = await requestSourceConnect(WS, SRC);
    expect(result.ok === false && result.error).toBe("unreachable");
    expect(connectRefusalCopy("unreachable")).toMatch(/Nothing changed/i);
  });

  it("never tells someone a refusal succeeded", () => {
    for (const reason of ["not found", "unreachable", "malformed_authorization_url", "??"]) {
      expect(connectRefusalCopy(reason)).not.toMatch(/\bconnected\b/i);
    }
  });
});
