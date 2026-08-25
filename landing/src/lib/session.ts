/**
 * Session — an opaque, server-resolved token. Edge Runtime compatible.
 *
 * ── What this replaces, and why it is a replacement rather than an edit ─────
 *
 * Until now the session was a self-contained HS256 JWT carrying
 * `{ userId: number, activeChatId: number | null }` — a Telegram user id and a
 * Telegram chat id, signed with a secret this tier holds.
 *
 * The target schema (`session_tokens`, migration 060) specifies the opposite
 * shape: an opaque value whose SHA256 is a row, with `expires_at` sliding on
 * use and a `revoked_at` column. Two properties follow that a JWT cannot have
 * at any price:
 *
 *   1. It can be REVOKED. A JWT is valid until it expires because there is
 *      nothing to mark; sign-out can only delete the cookie, which does nothing
 *      to a copy of it.
 *   2. It can name a UUID user. `users.id` is a UUID; `userId: number` cannot
 *      hold one.
 *
 * So there is no edit that turns one into the other, and every session minted
 * under the old shape names a user id in the `legacy` schema that the target
 * cannot resolve. Those sessions already point at nothing; failing to resolve
 * them makes that visible rather than causing it.
 *
 * The cookie NAME is deliberately unchanged, so a fresh sign-in overwrites the
 * old value rather than leaving it beside a new one under a different key.
 *
 * CORRECTION, and it is worth keeping rather than quietly editing: this comment
 * used to claim a stale JWT "fails to resolve and is cleared". Only the first
 * half was true. `resolveSessionToken` turns a 401/403 into null and the page
 * redirects to /login, which fails closed and is correct — but nothing deleted
 * the cookie, so it sat in the browser and was re-sent and re-rejected on every
 * request until an explicit sign-out. "Ignored forever, quietly" is not the
 * same claim as "cleared", and the difference is checkable: `SESSION_COOKIE` is
 * deleted in exactly one place, the logout route.
 *
 * `middleware.ts` now clears the one population that can be recognised without
 * a network call — see there. A stale OPAQUE token still cannot be recognised
 * locally and is still only ignored; that is stated rather than papered over.
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
}

export const SESSION_COOKIE = "storydump_session";

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

/** 30 days, matching `session_tokens.expires_at` (05 seam). */
const SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 30;

export const SESSION_COOKIE_OPTIONS = {
  httpOnly: true,
  secure: process.env.NODE_ENV === "production",
  sameSite: "lax" as const,
  path: "/",
  maxAge: SESSION_MAX_AGE_SECONDS,
};

/** Same options, minus httpOnly — no secret in it, and the client reads it. */
export const WORKSPACE_COOKIE_OPTIONS = {
  ...SESSION_COOKIE_OPTIONS,
  httpOnly: false,
};

type ResolvedSession = {
  user_id: string;
  display_name?: string;
  email?: string;
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
  const result = await targetFetch<ResolvedSession>("/auth/session", token);

  if (result.ok) {
    return {
      userId: result.data.user_id,
      displayName: result.data.display_name?.trim() || "",
      email: result.data.email,
      activeWorkspaceId: null,
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
  return (
    typeof value === "string" &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)
  );
}

/**
 * Does this session have a workspace to act in?
 *
 * Replaces `hasTenant()`, which asked `activeChatId !== null` — a question that
 * only ever meant "did a Telegram group create you". On the target schema a
 * workspace has no chat id at all, so the two questions have come apart and
 * only this one is answerable.
 *
 * FALSE IS NOT AN ERROR. It is the state of every user between signing in and
 * creating their first workspace.
 */
export function hasWorkspace(session: SessionUser | null): boolean {
  return Boolean(session && session.activeWorkspaceId);
}
