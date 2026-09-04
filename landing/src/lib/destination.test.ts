/**
 * The destination legs, browser half: a destination is added by CONNECTING
 * (owner ruling 2026-09-04), and an existing one is connected or reconnected
 * per row. The weight is on the line before navigation — only Instagram's own
 * authorize host is ever assigned to the window.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  connectControlFor,
  destinationConnectRefusalCopy,
  destinationConnectionCaption,
  destinationHandle,
  destinationIsActive,
  destinationStateBadge,
  isInstagramAuthorizationUrl,
  requestDestinationConnect,
  requestWorkspaceConnect,
} from "./destination";

const WS = "11111111-1111-4111-8111-111111111111";

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

describe("destinationHandle", () => {
  it("returns the typed handle", () => {
    expect(destinationHandle("thehandle")).toBe("thehandle");
  });

  // The point of the helper: a manual destination's reference is
  // `manual:thehandle`, and falling back to it would render an internal
  // convention on a settings screen.
  it("returns null rather than a placeholder when there is no handle", () => {
    expect(destinationHandle(null)).toBeNull();
    expect(destinationHandle(undefined)).toBeNull();
    expect(destinationHandle("   ")).toBeNull();
  });
});

describe("destinationIsActive", () => {
  it("is true only for the state the clock acts on", () => {
    expect(destinationIsActive("active")).toBe(true);
    expect(destinationIsActive("reauth_required")).toBe(false);
    expect(destinationIsActive("moved")).toBe(false);
    expect(destinationIsActive(undefined)).toBe(false);
  });
});

/**
 * #1121 Step 0. The payload has carried `state` since #1092 and the screen
 * collapsed it back to `state === "active"`, so the three non-active values all
 * rendered as the ABSENCE of a badge — indistinguishable from each other, and
 * from a component that had not loaded.
 */
describe("destinationStateBadge", () => {
  // `ck_ig_accounts_state`. Written out because the point of these tests is
  // that the UI covers the WHOLE vocabulary, and deriving the list from the
  // function under test would make that vacuous.
  const VOCABULARY = ["active", "reauth_required", "disabled", "moved"];

  it("gives every state in the vocabulary its own label", () => {
    const labels = VOCABULARY.map((s) => destinationStateBadge(s).label);
    // The property, not four assertions about instances: any two states
    // sharing a label is the regression, whichever pair it is.
    expect(new Set(labels).size).toBe(VOCABULARY.length);
  });

  it("renders something for a state it does not recognise", () => {
    // The load-bearing one. Absence is what made this a regression rather
    // than a gap, so no input may produce an empty badge.
    for (const state of [null, undefined, "", "some_future_state"]) {
      expect(destinationStateBadge(state).label).not.toBe("");
    }
  });

  it("flags an unrecognised state for attention rather than treating it as inert", () => {
    // The vocabulary is closed by a CHECK constraint, so a value outside it
    // means the schema moved or the payload is wrong — both worth looking at.
    expect(destinationStateBadge("some_future_state").tone).toBe("attention");
  });

  it("distinguishes disabled from moved by LABEL, not by colour alone", () => {
    const disabled = destinationStateBadge("disabled");
    const moved = destinationStateBadge("moved");
    expect(disabled.tone).toBe(moved.tone); // neither is actionable
    expect(disabled.label).not.toBe(moved.label); // and they are still tellable apart
  });

  it("marks the one state a person can act on", () => {
    expect(destinationStateBadge("reauth_required").tone).toBe("attention");
    expect(destinationStateBadge("active").tone).toBe("active");
  });
});

describe("isInstagramAuthorizationUrl", () => {
  // The value is NAVIGATED TO, so the guard sits at the navigation. Equality on
  // the host, never `endsWith` — same trap `source-connect.ts` names.
  it("accepts Instagram's authorize host over https", () => {
    expect(
      isInstagramAuthorizationUrl("https://api.instagram.com/oauth/authorize?client_id=1"),
    ).toBe(true);
  });

  it.each([
    "http://api.instagram.com/oauth/authorize",
    "https://evil-api.instagram.com/oauth/authorize",
    "https://api.instagram.com.evil.example/oauth/authorize",
    "https://api.instagram.com:8443/oauth/authorize",
    "https://accounts.google.com/o/oauth2/v2/auth",
    "not a url",
  ])("refuses %s", (value) => {
    expect(isInstagramAuthorizationUrl(value)).toBe(false);
  });
});

describe("connectControlFor", () => {
  it("offers Connect for a destination that has never been connected", () => {
    expect(connectControlFor("none")).toEqual({ label: "Connect Instagram", kind: "connect" });
  });

  it("offers Reconnect when the credential is expired or revoked", () => {
    expect(connectControlFor("expired")).toEqual({ label: "Reconnect Instagram", kind: "reconnect" });
    expect(connectControlFor("revoked")).toEqual({ label: "Reconnect Instagram", kind: "reconnect" });
  });

  it("offers nothing on a live connection — the badge says connected", () => {
    expect(connectControlFor("active")).toBeNull();
  });

  it("treats a value this build does not recognise as not connected", () => {
    expect(connectControlFor(undefined)).toEqual({ label: "Connect Instagram", kind: "connect" });
  });
});

describe("requestDestinationConnect", () => {
  const ACCOUNT = "55555555-5555-4555-8555-555555555555";

  it("asks the proxy for this destination's authorization URL and returns it", async () => {
    stubFetch({ authorizationUrl: "https://api.instagram.com/oauth/authorize?state=s" });
    const result = await requestDestinationConnect(WS, ACCOUNT);
    expect(captured[0].url).toBe(`/api/workspaces/${WS}/accounts/${ACCOUNT}/connect`);
    expect(captured[0].init.method).toBe("POST");
    expect(result).toEqual({
      ok: true,
      authorizationUrl: "https://api.instagram.com/oauth/authorize?state=s",
    });
  });

  it("refuses a URL that is not Instagram's, at the line before navigation", async () => {
    stubFetch({ authorizationUrl: "https://evil.example/oauth/authorize" });
    const result = await requestDestinationConnect(WS, ACCOUNT);
    expect(result).toEqual({ ok: false, error: "malformed_authorization_url", status: 200 });
  });

  it("carries the proxy's refusal by name", async () => {
    stubFetch({ error: "not found" }, 404);
    const result = await requestDestinationConnect(WS, ACCOUNT);
    expect(result).toEqual({ ok: false, error: "not found", status: 404 });
  });

  it("reports an unreachable app as such", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("offline"); }));
    const result = await requestDestinationConnect(WS, ACCOUNT);
    expect(result).toEqual({ ok: false, error: "unreachable", status: 0 });
  });
});

describe("requestWorkspaceConnect", () => {
  it("asks the proxy for the workspace's authorization URL — no account named", async () => {
    stubFetch({ authorizationUrl: "https://api.instagram.com/oauth/authorize?state=s" });
    const result = await requestWorkspaceConnect(WS);
    expect(captured[0].url).toBe(`/api/workspaces/${WS}/accounts/connect`);
    expect(captured[0].init.method).toBe("POST");
    expect(result).toEqual({
      ok: true,
      authorizationUrl: "https://api.instagram.com/oauth/authorize?state=s",
    });
  });

  it("refuses a URL that is not Instagram's, at the line before navigation", async () => {
    stubFetch({ authorizationUrl: "https://evil.example/oauth/authorize" });
    const result = await requestWorkspaceConnect(WS);
    expect(result).toEqual({ ok: false, error: "malformed_authorization_url", status: 200 });
  });

  it("carries the proxy's refusal by name", async () => {
    stubFetch({ error: "insufficient_role" }, 403);
    const result = await requestWorkspaceConnect(WS);
    expect(result).toEqual({ ok: false, error: "insufficient_role", status: 403 });
  });

  it("reports an unreachable app as such", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("offline"); }));
    const result = await requestWorkspaceConnect(WS);
    expect(result).toEqual({ ok: false, error: "unreachable", status: 0 });
  });
});

describe("destinationConnectRefusalCopy", () => {
  it("says the service is not set up when Instagram is unconfigured", () => {
    expect(destinationConnectRefusalCopy("http_503")).toMatch(/not set up/i);
  });

  it("names the admin floor on a role refusal", () => {
    expect(destinationConnectRefusalCopy("http_403")).toMatch(/admin/i);
  });

  it("sends a stale screen back for a destination that is gone", () => {
    expect(destinationConnectRefusalCopy("http_404")).toMatch(/reload/i);
  });

  it("has a sentence for the unknown case that promises nothing", () => {
    expect(destinationConnectRefusalCopy("mystery")).toMatch(/nothing changed/i);
  });
});

describe("destinationConnectionCaption", () => {
  it("says connected only for a live credential", () => {
    expect(destinationConnectionCaption("active")).toBe("Instagram connected");
  });

  it("says reconnect for an expired or revoked one", () => {
    expect(destinationConnectionCaption("expired")).toMatch(/reconnect/i);
    expect(destinationConnectionCaption("revoked")).toMatch(/reconnect/i);
  });

  it("says posting is by hand when nothing is connected", () => {
    expect(destinationConnectionCaption("none")).toMatch(/by hand/i);
    expect(destinationConnectionCaption(undefined)).toMatch(/by hand/i);
  });
});
