/**
 * Web sign-up — feature flag and the two seams this work depends on.
 *
 * Everything for Google-rooted sign-up is gated behind WEB_SIGNUP_ENABLED and
 * is OFF by default, so this whole surface is inert in production until the
 * two dependencies below exist. Server-side name deliberately: an auth-adjacent
 * flag must never reach the browser bundle as NEXT_PUBLIC_*.
 *
 * ── SEAM 1 — the session contract (owner: alex, not built) ──────────────────
 *
 * The credential design is `sd1.{user_uuid}.{tenant_uuid}.{issued_at}.{nonce}.{hmac}`.
 * It names a tenant. Under the lazy-minting ruling a freshly signed-up user has
 * NO tenant, so there is a shape this format cannot currently express.
 *
 * `hasTenant()` below is the ONLY place this codebase asks that question. When
 * the session contract lands it should be the only edit needed. It is written
 * against today's Telegram-shaped session and returns false for anything else,
 * which is the safe direction: a user is treated as tenant-less until something
 * positively says otherwise.
 *
 * ── SEAM 2 — the tenant provisioning door (owner: nobody yet, not built) ────
 *
 * Tenant minting is an explicit act at a provisioning door and MUST NOT happen
 * in auth, in middleware, or as a side effect of resolving. The existing
 * `ChatSettingsRepository.get_or_create` CANNOT serve this case: it is keyed on
 * telegram_chat_id, so handed NULL it never finds a row and is create-always —
 * one fresh tenant per request. It is deliberately not called from here.
 *
 * What the web surface needs, stated so the door can be built against it:
 *
 *   ensure_personal_tenant(user_id) -> tenant_id
 *     - keyed on user_id (the only identity a web tenant has)
 *     - idempotent under concurrency (two racing calls -> one row, same id)
 *     - mints the owner membership in the same transaction
 *     - mints with flags that keep a NULL-chat tenant OUT of the scheduler
 *       sweep (get_all_active), which today's defaults do not
 *
 * Until it exists, every tenant-scoped action on the new screens is marked
 * `TENANT_DOOR_REQUIRED` at its call site and rendered as unavailable rather
 * than wired to something that would appear to work.
 */

import type { SessionPayload } from "./session";

/** Off unless a deploy explicitly turns it on. Never NEXT_PUBLIC_*. */
export function webSignupEnabled(): boolean {
  return process.env.WEB_SIGNUP_ENABLED === "true";
}

/**
 * Does this session have a tenant the backend can serve?
 *
 * SEAM 1. Today the only tenant a session can name is a Telegram chat, so this
 * is `activeChatId !== null`. A Google-rooted session carries no such field
 * yet — it will report false and be routed to the tenant-less surfaces, which
 * is correct behaviour rather than a placeholder.
 */
export function hasTenant(session: SessionPayload | null): boolean {
  if (!session) return false;
  return session.activeChatId !== null && session.activeChatId !== undefined;
}

/**
 * Marks a call site that needs SEAM 2 and must not be wired until it exists.
 * Exported so the sites are greppable: `grep -rn TENANT_DOOR_REQUIRED src/`.
 */
export const TENANT_DOOR_REQUIRED = "ensure_personal_tenant(user_id) — not built" as const;
