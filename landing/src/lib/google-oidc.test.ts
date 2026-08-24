/**
 * #1015 — the gates that decide whether Google sign-in is offered at all, and
 * the state cookie that makes the callback trustworthy.
 *
 * These are the security-critical PURE parts. The network halves
 * (`exchangeAndVerify`) are not covered here: they are a fetch plus jose's own
 * JWKS verification, and a test that mocks both proves the mock. What is
 * covered is everything that decides, which is where our own bugs live.
 */

import { beforeEach, describe, expect, it } from "vitest";
import {
  SUBJECT_STORAGE_AVAILABLE,
  beginAuth,
  googleSigninAvailable,
  googleSigninConfigured,
  originIsRegistered,
  readStateToken,
} from "./google-oidc";

const REGISTERED = "https://storydump.app";

function configure() {
  process.env.GOOGLE_CLIENT_ID = "test-client-id.apps.googleusercontent.com";
  process.env.GOOGLE_CLIENT_SECRET = "test-secret-not-a-real-one";
  process.env.GOOGLE_SIGNIN_REDIRECT_BASE = REGISTERED;
  process.env.JWT_SECRET = "0123456789abcdef0123456789abcdef";
}

beforeEach(() => {
  delete process.env.GOOGLE_CLIENT_ID;
  delete process.env.GOOGLE_CLIENT_SECRET;
  delete process.env.GOOGLE_SIGNIN_REDIRECT_BASE;
  delete process.env.JWT_SECRET;
});

describe("the origin gate", () => {
  it("accepts only the exact registered origin", () => {
    configure();
    expect(originIsRegistered(REGISTERED)).toBe(true);
  });

  it("rejects a preview alias, which is the case it exists for", () => {
    configure();
    expect(
      originIsRegistered("https://storydump-git-some-branch-chrisrogers37.vercel.app"),
    ).toBe(false);
  });

  it("rejects www when the apex is registered", () => {
    // Google matches host exactly, so these are different redirect URIs. This
    // is also why www should 301 to the apex rather than be registered too:
    // the session cookie is host-only, so two live hosts hold two sessions.
    configure();
    expect(originIsRegistered("https://www.storydump.app")).toBe(false);
  });

  it("rejects a different scheme on the same host", () => {
    configure();
    expect(originIsRegistered("http://storydump.app")).toBe(false);
  });

  it("rejects unparseable input and a null origin rather than throwing", () => {
    configure();
    expect(originIsRegistered("not-a-url")).toBe(false);
    expect(originIsRegistered(null)).toBe(false);
  });

  it("rejects everything when no redirect base is configured", () => {
    expect(originIsRegistered(REGISTERED)).toBe(false);
  });
});

describe("googleSigninConfigured", () => {
  it("needs all three, and says no on any one missing", () => {
    configure();
    expect(googleSigninConfigured()).toBe(true);
    for (const k of [
      "GOOGLE_CLIENT_ID",
      "GOOGLE_CLIENT_SECRET",
      "GOOGLE_SIGNIN_REDIRECT_BASE",
    ]) {
      configure();
      delete process.env[k];
      expect(googleSigninConfigured(), `missing ${k}`).toBe(false);
    }
  });
});

describe("the storage boundary", () => {
  it("is closed, and closing it is what keeps the button off", () => {
    // Pins the boundary itself. When the identity schema lands and this flips,
    // THIS test is the one that must be updated deliberately — which is the
    // point: it makes the flip a decision rather than a side effect.
    expect(SUBJECT_STORAGE_AVAILABLE).toBe(false);
  });

  it("makes sign-in unavailable even when fully configured on the right origin", () => {
    configure();
    expect(googleSigninConfigured()).toBe(true);
    expect(originIsRegistered(REGISTERED)).toBe(true);
    // Both preconditions hold and it is STILL unavailable. If this ever passes
    // for the wrong reason, the two assertions above say which one moved.
    expect(googleSigninAvailable(REGISTERED)).toBe(false);
  });
});

describe("the state cookie", () => {
  it("round-trips state and nonce, and they differ", async () => {
    configure();
    const { stateToken } = await beginAuth();
    const read = await readStateToken(stateToken);
    expect(read).not.toBeNull();
    expect(read!.state).toBeTruthy();
    expect(read!.nonce).toBeTruthy();
    // Distinct values on purpose: state is CSRF, checked against the query;
    // nonce binds the ID token and is checked inside its claims. One value
    // doing both jobs would let a replayed token satisfy the wrong check.
    expect(read!.state).not.toEqual(read!.nonce);
  });

  it("issues a fresh state and nonce every time", async () => {
    configure();
    const a = await readStateToken((await beginAuth()).stateToken);
    const b = await readStateToken((await beginAuth()).stateToken);
    expect(a!.state).not.toEqual(b!.state);
    expect(a!.nonce).not.toEqual(b!.nonce);
  });

  it("refuses a tampered token", async () => {
    configure();
    const { stateToken } = await beginAuth();
    const [h, p, s] = stateToken.split(".");
    const flipped = s.slice(0, -1) + (s.endsWith("A") ? "B" : "A");
    expect(await readStateToken(`${h}.${p}.${flipped}`)).toBeNull();
  });

  it("refuses a token signed with a different secret", async () => {
    configure();
    const { stateToken } = await beginAuth();
    process.env.JWT_SECRET = "ffffffffffffffffffffffffffffffff";
    expect(await readStateToken(stateToken)).toBeNull();
  });

  it("refuses absent and malformed input rather than throwing", async () => {
    configure();
    expect(await readStateToken(undefined)).toBeNull();
    expect(await readStateToken("")).toBeNull();
    expect(await readStateToken("not.a.jwt")).toBeNull();
  });

  it("is not interchangeable with a session token", async () => {
    // The state key is HMAC-derived from JWT_SECRET rather than being it, so a
    // session token cannot be presented as state. Without that separation the
    // two are the same signature and this passes for the wrong reason.
    configure();
    const { SignJWT } = await import("jose");
    const sessionish = await new SignJWT({ state: "x", nonce: "y" })
      .setProtectedHeader({ alg: "HS256" })
      .setIssuedAt()
      .setExpirationTime("10m")
      .sign(new TextEncoder().encode(process.env.JWT_SECRET as string));
    expect(await readStateToken(sessionish)).toBeNull();
  });
});

describe("the authorization request", () => {
  it("asks for identity scopes only, and not Drive", async () => {
    configure();
    const { authUrl } = await beginAuth();
    const scope = new URL(authUrl).searchParams.get("scope") ?? "";
    expect(scope.split(" ").sort()).toEqual(["email", "openid", "profile"]);
    expect(scope).not.toContain("drive");
  });

  it("does not request offline access or force a consent screen", async () => {
    // Drive's flow sets access_type=offline and prompt=consent to guarantee a
    // refresh token. Sign-in wants neither: no refresh token is kept, and
    // prompt=consent would re-prompt a returning user on every login. Both are
    // per-request, which is why one client can serve both.
    configure();
    const params = new URL((await beginAuth()).authUrl).searchParams;
    expect(params.get("access_type")).toBe("online");
    expect(params.get("prompt")).toBe("select_account");
  });

  it("sends the configured redirect URI, never one derived from a request", async () => {
    configure();
    const params = new URL((await beginAuth()).authUrl).searchParams;
    expect(params.get("redirect_uri")).toBe(`${REGISTERED}/auth/google/callback`);
  });

  it("carries the same state and nonce it put in the cookie", async () => {
    configure();
    const { authUrl, stateToken } = await beginAuth();
    const params = new URL(authUrl).searchParams;
    const cookie = await readStateToken(stateToken);
    expect(params.get("state")).toBe(cookie!.state);
    expect(params.get("nonce")).toBe(cookie!.nonce);
  });
});
