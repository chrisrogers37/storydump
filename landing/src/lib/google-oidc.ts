/**
 * Google OIDC sign-in — the entry point half (#1015).
 *
 * Starts the flow, verifies what Google returns, and hands back a verified
 * subject. It does not itself create a user or mint a session — the callback
 * does that through the target router, which is where the write belongs.
 *
 * ── The storage boundary is GONE ────────────────────────────────────────────
 *
 * It was real and it is worth recording what closed it, because a stale version
 * of this comment is what kept the button switched off for a day after it
 * stopped being true.
 *
 * A returning Google user is recognised by their OIDC `sub`, and until the M.3
 * cutover there was nowhere to put one: `user_identities` was migration 053 and
 * unadopted, `users` had no subject column, and `users.telegram_user_id` was
 * NOT NULL so a Google-only row could not be inserted at all.
 *
 * The cutover applied migrations 051-063 on 2026-08-24 at 02:20:14Z. In the
 * target schema `users` has NO telegram column, and `user_identities` exists
 * with `provider CHECK IN ('telegram','google')`, `external_id`, and
 * `UNIQUE (provider, external_id)` — which is precisely a home for a `sub`.
 *
 * So `SUBJECT_STORAGE_AVAILABLE` is true, and `googleSigninAvailable()` follows
 * it as its own comment promised it would.
 *
 * ── Why the state and nonce live in a cookie, not `oauth_states` ────────────
 *
 * `07-security-model.md` §2 gives one state table four purposes — connect,
 * reconnect, signin, link. Three of them bind a state row to an existing user,
 * workspace or credential owner and genuinely need server state. `signin` is
 * the one purpose that is ANONYMOUS by construction (the plan's own words), so
 * all it must carry is CSRF state and the ID-token nonce — both of which a
 * short-lived signed httpOnly cookie carries correctly, with nothing to sweep
 * and nothing to leak. That table is migration 060 and is also unadopted; when
 * it lands this can move onto it without a format change.
 */

import { createHmac, randomBytes } from "crypto";
import { SignJWT, jwtVerify, createRemoteJWKSet } from "jose";

/** Google's OIDC issuer, as it appears in the `iss` claim. */
const GOOGLE_ISSUER = "https://accounts.google.com";
const GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth";
const GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token";
const GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs";

/** Identity only. Drive access is a separate, later, incremental request. */
const SIGNIN_SCOPES = ["openid", "email", "profile"];

export const STATE_COOKIE = "storydump_oidc_state";
/** Long enough for a real person to complete consent, short enough to be cheap. */
const STATE_TTL_SECONDS = 600;

export const CALLBACK_PATH = "/auth/google/callback";

/**
 * TRUE — `user_identities` is live and is where the subject goes.
 *
 * Kept as a named constant rather than deleted, because it is the one place
 * that states WHICH schema fact the sign-in button depends on. It was false for
 * a real reason and flipped for a real reason; a bare `true` inlined at the
 * call site would lose both.
 *
 * Greppable on purpose: `grep -rn SUBJECT_STORAGE_AVAILABLE landing/src/`.
 */
export const SUBJECT_STORAGE_AVAILABLE = true;

export type VerifiedSubject = {
  /** The provider's immutable subject. Identity keys on THIS, never email. */
  sub: string;
  email?: string;
  emailVerified: boolean;
  name?: string;
};

export function googleSigninConfigured(): boolean {
  return Boolean(
    process.env.GOOGLE_CLIENT_ID &&
    process.env.GOOGLE_CLIENT_SECRET &&
    process.env.GOOGLE_SIGNIN_REDIRECT_BASE,
  );
}

/**
 * The exact redirect URI, which must match what is registered byte for byte.
 *
 * Derived from one configured base rather than from the request's own Host, and
 * that is a safety property: taking it from the request would let a Host header
 * choose where the code is delivered. It is also why previews do not silently
 * get a working button — see `originIsRegistered`.
 */
export function signinRedirectUri(): string {
  const base = (process.env.GOOGLE_SIGNIN_REDIRECT_BASE || "").replace(
    /\/+$/,
    "",
  );
  return `${base}${CALLBACK_PATH}`;
}

/**
 * Is THIS origin one Google will actually redirect back to?
 *
 * Google matches redirect URIs exactly, host included, and forbids wildcards.
 * A preview deployment therefore has no working sign-in unless its alias is
 * registered — so the button must not render there. Rendering it anyway
 * produces `redirect_uri_mismatch`, which no amount of reading our code
 * explains; virgil's rule that a button which 404s is worse than no button is
 * the same rule.
 */
export function originIsRegistered(currentOrigin: string | null): boolean {
  const base = process.env.GOOGLE_SIGNIN_REDIRECT_BASE;
  if (!base || !currentOrigin) return false;
  try {
    return new URL(base).origin === new URL(currentOrigin).origin;
  } catch {
    return false;
  }
}

/** Every condition that must hold before the button may be shown. */
export function googleSigninAvailable(currentOrigin: string | null): boolean {
  return (
    SUBJECT_STORAGE_AVAILABLE &&
    googleSigninConfigured() &&
    originIsRegistered(currentOrigin)
  );
}

/**
 * A key for the state cookie, derived from JWT_SECRET rather than a new secret.
 *
 * Domain-separated with HMAC so a state token can never be presented as a
 * session token or the reverse, which is the property that matters — the same
 * derivation `webapp_auth` uses for its two Telegram credentials. Reusing the
 * secret directly would make the two interchangeable.
 */
function stateKey(): Uint8Array {
  const raw = process.env.JWT_SECRET;
  if (!raw || raw.length < 32) {
    throw new Error("JWT_SECRET must be set to a random 32+ character string");
  }
  return new Uint8Array(createHmac("sha256", raw).update("OidcState").digest());
}

export type AuthStart = { authUrl: string; stateToken: string };

/**
 * Begin the flow: fresh state + nonce, the authorization URL, and the token to
 * put in the cookie.
 *
 * `state` is CSRF: it comes back in the query and must equal what the cookie
 * holds. `nonce` binds the ID TOKEN to this same request and is checked inside
 * the token's claims, which is a different attack and needs the separate value.
 */
export async function beginAuth(): Promise<AuthStart> {
  const state = randomBytes(32).toString("base64url");
  const nonce = randomBytes(32).toString("base64url");

  const stateToken = await new SignJWT({ state, nonce })
    .setProtectedHeader({ alg: "HS256" })
    .setIssuedAt()
    .setExpirationTime(`${STATE_TTL_SECONDS}s`)
    .sign(stateKey());

  const params = new URLSearchParams({
    client_id: process.env.GOOGLE_CLIENT_ID as string,
    redirect_uri: signinRedirectUri(),
    response_type: "code",
    scope: SIGNIN_SCOPES.join(" "),
    state,
    nonce,
    // Sign-in must not re-prompt a returning user, and must not ask for
    // offline access: no refresh token is wanted here. Drive's flow sets both
    // and is a different request against the same client.
    prompt: "select_account",
    access_type: "online",
  });

  return { authUrl: `${GOOGLE_AUTH_URL}?${params.toString()}`, stateToken };
}

/** Read back the state cookie. Returns null on anything unverifiable. */
export async function readStateToken(
  token: string | undefined,
): Promise<{ state: string; nonce: string } | null> {
  if (!token) return null;
  try {
    const { payload } = await jwtVerify(token, stateKey());
    const state = payload.state as string | undefined;
    const nonce = payload.nonce as string | undefined;
    if (!state || !nonce) return null;
    return { state, nonce };
  } catch {
    return null;
  }
}

const jwks = createRemoteJWKSet(new URL(GOOGLE_JWKS_URL));

/**
 * Exchange the code and verify the ID token, returning the verified subject.
 *
 * Verification is jose's, against Google's published JWKS, with issuer and
 * audience pinned. The nonce is checked HERE rather than trusted from the
 * exchange: it is the claim that ties this token to this browser's request.
 *
 * `email_verified` is carried but NOT used as identity (D32). Identity is
 * `sub`, always — an email is mutable and reassignable, so keying on it is an
 * account-takeover primitive.
 */
export async function exchangeAndVerify(
  code: string,
  expectedNonce: string,
): Promise<VerifiedSubject> {
  const body = new URLSearchParams({
    code,
    client_id: process.env.GOOGLE_CLIENT_ID as string,
    client_secret: process.env.GOOGLE_CLIENT_SECRET as string,
    redirect_uri: signinRedirectUri(),
    grant_type: "authorization_code",
  });

  const res = await fetch(GOOGLE_TOKEN_URL, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
  });
  if (!res.ok) {
    // The body can echo request parameters; the status is what a caller needs.
    throw new Error(`Token exchange failed with status ${res.status}`);
  }

  const tokens = (await res.json()) as { id_token?: string };
  if (!tokens.id_token) throw new Error("Token response carried no id_token");

  const { payload } = await jwtVerify(tokens.id_token, jwks, {
    issuer: GOOGLE_ISSUER,
    audience: process.env.GOOGLE_CLIENT_ID as string,
  });

  if (payload.nonce !== expectedNonce) {
    throw new Error("ID token nonce did not match the request");
  }
  const sub = payload.sub;
  if (!sub) throw new Error("ID token carried no sub");

  return {
    sub,
    email: payload.email as string | undefined,
    emailVerified: payload.email_verified === true,
    name: payload.name as string | undefined,
  };
}
