/**
 * Session — an opaque, server-resolved token. Edge Runtime compatible.
 *
 * ── Why an opaque token rather than a JWT ───────────────────────────────────
 *
 * The schema (`session_tokens`, `auth_plane.py`) makes a session an opaque value
 * whose SHA256 is a row, with `expires_at` sliding on use and a `revoked_at`
 * column. Two properties follow that a self-contained JWT cannot have at any
 * price:
 *
 * 1. It can be REVOKED. A JWT is valid until it expires because there is
 * nothing to mark; sign-out can only delete the cookie, which does nothing
 * to a copy of it.
 * 2. It can name a UUID user. `users.id` is a UUID; a numeric `userId`
 * cannot hold one.
 *
 * THE COOKIE NAME IS THE API'S. Sign-in is API-hosted and nothing here
 * writes the session, so this tier reads the cookie under the name the API
 * writes it; a name of its own would leave the gate blind to a real session
 * and bounce a completed sign-in to /login.
 *
 * A stale token fails to resolve: `resolveSessionToken` turns a 401/403 into
 * null and the page redirects to /login, which fails closed. That does not
 * delete the cookie: `SESSION_COOKIE` is deleted in exactly one place, the
 * logout route.
 *
 * `middleware.ts` clears the one population that can be recognised without a
 * network call — see there. A stale OPAQUE token cannot be recognised locally
 * and is only ignored; that is stated rather than papered over.
 *
 * ── Why the token is not verified locally ──────────────────────────────────
 *
 * There is nothing to verify against: the token is opaque and its hash lives in
 * a table this tier has no connection to. Resolution is therefore a call, and
 * that is the point — a revoked token stops working immediately, which is the
 * property revocation exists for.
 */

import { cache } from "react";
import { cookies } from "next/headers";
import { targetFetch } from "./target-api";
import { telegramIdentityFrom } from "./telegram-link";

export interface SessionUser {
  /** `users.id` — a UUID. */
  userId: string;
  displayName: string;
  email?: string;
  /**
   * The workspace the UI is currently pointed at, or null.
   *
   * NULL IS A NORMAL STATE, NOT AN ERROR — it is every user's state between
   * signing in and creating their first workspace, and `/welcome` and
   * `/workspaces` both render in it.
   */
  activeWorkspaceId: string | null;
  /**
   * The memberships, or NULL when the API could not read them.
   *
   * Null and `[]` are different answers with opposite remedies, which is why
   * this is not narrowed to an array — see `MeResponse.workspaces`.
   */
  workspaces: Array<{ id: string; name: string; role: string; state: string }> | null;
  /** Reasons the API named for anything it could not fully answer. */
  degraded: string[];
  /** Whether a Telegram identity is attached (`/me` identities, provider `telegram`). */
  telegramLinked: boolean;
  /** That identity's display name, or null — linked-without-a-name is a real state. */
  telegramDisplayName: string | null;
  /**
   * Whether the API counts this person among the deployment's operators
   * (`/me`'s `ops`, `OPS_USER_IDS`;). It decides two things here and no
   * more: whether `/ops` renders or is the site's 404, and whether the
   * dashboard header links to it. Nothing is read on its strength: every
   * owner route is the API's to refuse (`require_ops`).
   */
  isOps: boolean;
}

/**
 * The session cookie. The API is its SSOT — `src/api/principal.py` (`COOKIE`).
 *
 * This tier READS it and never writes it: sign-in is API-hosted, so the value is
 * minted, set and revoked there. Two languages, so the name cannot be a shared
 * import — it is duplicated deliberately, and this comment is the pointer that
 * makes the duplication greppable from either side.
 *
 * The name being right is NECESSARY AND NOT SUFFICIENT. This tier can only read
 * the cookie when the API sets `SESSION_COOKIE_DOMAIN` to the shared registrable
 * domain; left at its default the cookie is host-only on the API's host and this
 * tier's server side cannot see it at all, under any name.
 */
export const SESSION_COOKIE = "sd_session";

/**
 * The selected workspace. A PREFERENCE, not a credential.
 *
 * It names a workspace; it does not grant anything. Every target call
 * authorizes against `workspace_members` server-side, so editing this cookie
 * by hand gets a 403 rather than access to someone else's workspace. It is
 * separate from the session token because it is per-browser UI state and
 * `session_tokens` has no column for it — which is the schema saying the same
 * thing.
 */
export const WORKSPACE_COOKIE = "storydump_workspace";

/** 30 days. This tier's own cookie, so this tier owns its lifetime. */
const WORKSPACE_MAX_AGE_SECONDS = 60 * 60 * 24 * 30;

/**
 * Not httpOnly — no secret in it, and the client reads it.
 *
 * Its own options, not a session-cookie block: this tier never writes the
 * session cookie, and a block describing that write would invite one.
 */
export const WORKSPACE_COOKIE_OPTIONS = {
  httpOnly: false,
  secure: process.env.NODE_ENV === "production",
  sameSite: "lax" as const,
  path: "/",
  maxAge: WORKSPACE_MAX_AGE_SECONDS,
};

/**
 * The invitation token, remembered just long enough to survive the round
 * trip to Google.
 *
 * A cookie rather than a `?next=` parameter: a `next` parameter is a
 * redirect target an attacker can set, and the fix for that is an allowlist
 * nobody maintains. A cookie holding only the token cannot name a
 * destination at all.
 *
 * Here rather than in `app/join/[token]/start/route.ts`: a route module is a
 * mounted endpoint, not a library, and the other two cookie names this tier
 * owns live here.
 */
export const INVITE_COOKIE = "storydump_invite";

/** 15 minutes. Long enough for a sign-in, short enough that a stale invite dies. */
export const INVITE_MAX_AGE_SECONDS = 60 * 15;

type MeResponse = {
  user: {
    id: string;
    display_name?: string;
    primary_email?: string;
    identities?: Array<{ provider: string; display_name?: string | null }>;
  } | null;
  /**
   * NULL IS NOT AN EMPTY LIST, and the API is explicit about the difference:
   * when the membership list cannot be read it answers `null` with the reason
   * in `degraded`, "so a front end can say 'cannot list your workspaces'
   * instead of 'you have none'."
   *
   * It is honoured here rather than flattened — `?? []` on this field would
   * undo the API's care at the last hop.
   */
  workspaces: Array<{ id: string; name: string; role: string; state: string }> | null;
  degraded: string[];
  /** Whether the person is in the API's `OPS_USER_IDS`; only `true` counts. */
  ops?: boolean;
};

/**
 * Resolve an opaque token to a user, or null.
 *
 * NULL MEANS "NOT A VALID SESSION" AND NOTHING ELSE. An unreachable router is
 * NOT null — it throws, so a caller cannot read "the router is down" as "you
 * are signed out" and silently bounce a signed-in user to the login page.
 * Those have opposite remedies and only one of them is the user's problem.
 */
export async function resolveSessionToken(
  token: string,
): Promise<SessionUser | null> {
  const result = await targetFetch<MeResponse>("/me", token);

  if (result.ok) {
    const user = result.data.user;
    if (!user) return null;
    return {
      userId: user.id,
      displayName: user.display_name?.trim() || "",
      email: user.primary_email,
      activeWorkspaceId: null,
      workspaces: result.data.workspaces,
      degraded: result.data.degraded ?? [],
      telegramLinked: telegramIdentityFrom(user.identities) !== null,
      telegramDisplayName: telegramIdentityFrom(user.identities)?.displayName ?? null,
      isOps: result.data.ops === true,
    };
  }

  // 401/403 — presented and rejected. That is a real "not signed in".
  if (result.status === 401 || result.status === 403) return null;

  throw new SessionUnavailableError(result.error, result.status);
}

/** The session could not be resolved — distinct from resolving to nobody. */
export class SessionUnavailableError extends Error {
  constructor(
    readonly reason: string,
    readonly status: number,
  ) {
    super(`session_unavailable: ${reason}`);
    this.name = "SessionUnavailableError";
  }
}

/**
 * The current session, deduped per request.
 *
 * Returns null when there is no valid session. Throws
 * `SessionUnavailableError` when the question could not be answered — callers
 * that want a login redirect should catch it deliberately rather than by
 * treating every failure as a signed-out user.
 */
export const getSession = cache(async (): Promise<SessionUser | null> => {
  const cookieStore = await cookies();
  const token = cookieStore.get(SESSION_COOKIE)?.value;
  if (!token) return null;

  const session = await resolveSessionToken(token);
  if (!session) return null;

  const selected = cookieStore.get(WORKSPACE_COOKIE)?.value;
  return {
    ...session,
    activeWorkspaceId: isWorkspaceId(selected) ? selected : null,
  };
});

/** Read the raw token for a call that forwards it. */
export async function getSessionToken(): Promise<string | null> {
  const cookieStore = await cookies();
  return cookieStore.get(SESSION_COOKIE)?.value ?? null;
}

/**
 * A UUID, shape only.
 *
 * This is not an authorization check and must never be read as one — it stops a
 * junk cookie becoming a junk path segment. Membership is the backend's.
 */
export function isWorkspaceId(value: string | undefined): value is string {
  return isUuid(value);
}

/**
 * The one UUID-shape predicate: every id this tier forwards to the API is
 * one or nothing.
 *
 * DELIBERATELY SHAPE-ONLY, not RFC-4122. A stricter predicate (version
 * nibble `[1-5]`, variant `[89ab]`) would add nothing: Postgres
 * `gen_random_uuid()` emits v4, so every id this tier will ever see passes
 * both — and the strict form REJECTS
 * `00000000-0000-0000-0000-000000000000`, which is a legal id shape and is
 * pinned in `session-guards.test.ts`. The job here is to stop a junk cookie
 * becoming a junk path segment; the API re-validates everything it is sent.
 */
export function isUuid(value: unknown): value is string {
  return (
    typeof value === "string" &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)
  );
}

/**
 * Does this session have a workspace to act in?
 *
 * FALSE IS NOT AN ERROR. It is the state of every user between signing in and
 * creating their first workspace.
 */
export function hasWorkspace(session: SessionUser | null): boolean {
  return Boolean(session && session.activeWorkspaceId);
}
