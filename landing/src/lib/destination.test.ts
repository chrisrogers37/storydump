/**
 * The destination add leg, browser half (#1089).
 *
 * The weight is on two things: that the client sends the HANDLE ALONE — the
 * provisional reference is the port's to derive, and a second copy of that
 * convention here is the copy that goes stale — and that `created: false` is
 * carried rather than flattened, because "added" and "you already had that one"
 * are different sentences.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  addDestination,
  addDestinationRefusalCopy,
  connectControlFor,
  destinationConnectRefusalCopy,
  destinationConnectionCaption,
  destinationHandle,
  destinationIsActive,
  destinationStateBadge,
  isInstagramAuthorizationUrl,
  requestDestinationConnect,
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

describe("addDestination", () => {
  it("posts the handle to the workspace's accounts route", async () => {
    stubFetch({ accountId: "acc-1", created: true }, 201);
    const result = await addDestination(WS, "thehandle");

    expect(result).toEqual({ ok: true, accountId: "acc-1", created: true });
    expect(captured).toHaveLength(1);
    expect(captured[0].url).toBe(`/api/workspaces/${WS}/accounts`);
    expect(captured[0].init.method).toBe("POST");
  });

  // The load-bearing one. If this body ever grows a `provider_account_ref`,
  // the `manual:` convention lives in two places and the browser has started
  // asserting an identity it cannot know.
  it("sends the handle ALONE — never a provider reference", async () => {
    stubFetch({ accountId: "acc-1", created: true }, 201);
    await addDestination(WS, "thehandle");

    const body = JSON.parse(String(captured[0].init.body));
    expect(body).toEqual({ handle: "thehandle" });
    expect(body).not.toHaveProperty("provider_account_ref");
  });

  it("carries created:false so a repeat can be told from a first add", async () => {
    stubFetch({ accountId: "acc-1", created: false }, 200);
    const result = await addDestination(WS, "thehandle");
    expect(result).toEqual({ ok: true, accountId: "acc-1", created: false });
  });

  it("surfaces the port's named refusal rather than a status code", async () => {
    stubFetch({ error: "handle_malformed" }, 400);
    const result = await addDestination(WS, "two words");
    expect(result).toEqual({ ok: false, error: "handle_malformed", status: 400 });
  });

  it("falls back to the status when no reason is carried", async () => {
    stubFetch({}, 500);
    const result = await addDestination(WS, "thehandle");
    expect(result).toEqual({ ok: false, error: "http_500", status: 500 });
  });

  it("reports an unreachable server distinctly from a refusal", async () => {
    captured = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("network");
      }),
    );
    const result = await addDestination(WS, "thehandle");
    expect(result).toEqual({ ok: false, error: "unreachable", status: 0 });
  });

  // A 200 with no id is a broken contract, not a success. Rendering it would
  // tell someone their destination was added when nothing came back to say so.
  it("refuses a success body with no account id", async () => {
    stubFetch({ created: true }, 201);
    const result = await addDestination(WS, "thehandle");
    expect(result).toEqual({ ok: false, error: "malformed_response", status: 201 });
  });
});

describe("addDestinationRefusalCopy", () => {
  it.each([
    ["handle_required", "Type the Instagram handle"],
    ["handle_malformed", "no spaces"],
    ["handle_too_long", "too long"],
    // #1140: was ["unauthenticated", "session expired"]. The describe block's
    // contract is "names what happened" and that row named something the code
    // never established — a 401 admits roughly seven causes and the sentence
    // picked one, wrongly, on the first real sign-in. The row is kept rather
    // than deleted because a fragment assertion here is still worth having;
    // what makes it real is that the fragment is now a description of the
    // OBSERVATION rather than a guess at the cause. The exhaustive pin across
    // all six sites lives in `refusal-copy.test.ts`.
    ["unauthenticated", "not signed in, or the app could not prove it"],
    ["insufficient_role", "admin of this workspace"],
    ["unreachable", "Nothing was added"],
  ])("%s names what happened", (reason, fragment) => {
    expect(addDestinationRefusalCopy(reason)).toContain(fragment);
  });

  it("has a sentence for a reason it has never seen", () => {
    expect(addDestinationRefusalCopy("something_new")).toContain("Nothing was added");
  });
});

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
