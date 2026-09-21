import { callBff, postJson, type BffResult } from "./bff";
import { notAuthenticatedCopy, unreachableCopy } from "./refusal-copy";

/**
 * API tokens, browser side (CLI v2 phase 01, spec §2).
 *
 * One token model, two principal kinds. A PERSON-BOUND token acts as the
 * person in every workspace they belong to, at their membership role or the
 * token's, whichever is lower; a WORKSPACE SERVICE IDENTITY belongs to the
 * workspace and, in this release, reads only. Both are minted here — from a
 * signed-in session, never by a token — shown ONCE, and stored by the API as
 * a hash. The CLI (`storydump login`) takes the secret from the person, not
 * from this tier.
 *
 * A token is a RESOURCE, so it is REST at the proxy (`/api/me/tokens`,
 * `/api/workspaces/{ws}/tokens`) and not a command — the F1 (b) split that
 * `sources/route.ts` explains. The API's rows are snake_case; the proxy
 * reshapes them to camelCase for the browser, and `tokenRowFrom` is the ONE
 * place that reshape lives, used by the proxy and by the settings page's own
 * server read alike, so the screen has one row shape whichever way it came.
 */

// ── The closed sets and limits, shared by the form and the proxy ───────────

export const TOKEN_ROLES = ["operator", "readonly"] as const;
export type TokenRole = (typeof TOKEN_ROLES)[number];

export function isTokenRole(value: unknown): value is TokenRole {
  return (
    typeof value === "string" &&
    (TOKEN_ROLES as readonly string[]).includes(value)
  );
}

/** `service_tokens.name` — 1–80 characters. */
export const TOKEN_NAME_MAX = 80;
export const EXPIRY_DAYS_MIN = 1;
export const EXPIRY_DAYS_MAX = 365;
/** The API's own default (`service_tokens.mint`, `expires_in_days=90`). */
export const EXPIRY_DAYS_DEFAULT = 90;

/** Shape only; the API refuses by name what this lets through. */
export function tokenNameValid(name: string): boolean {
  const trimmed = name.trim();
  return trimmed.length >= 1 && trimmed.length <= TOKEN_NAME_MAX;
}

/** A whole number of days, 1–365. Not a string, not a float. */
export function expiryDaysValid(value: unknown): value is number {
  return (
    typeof value === "number" &&
    Number.isInteger(value) &&
    value >= EXPIRY_DAYS_MIN &&
    value <= EXPIRY_DAYS_MAX
  );
}

/**
 * The prefix the API routes bearer values on: `sdt_` resolves through the
 * token table, anything else takes the session path. The remainder is
 * url-safe base64 (43 characters for the API's 32 random bytes); only the
 * prefix and the alphabet are pinned here, because refusing a real secret
 * over its length would strand a token that was minted and never shown.
 */
export const SECRET_PREFIX = "sdt_";
const SECRET_SHAPE = /^sdt_[A-Za-z0-9_-]+$/;

export function isTokenSecret(value: unknown): value is string {
  return typeof value === "string" && SECRET_SHAPE.test(value);
}

// ── The rows ───────────────────────────────────────────────────────────────

/** A token as the screen sees it — one shape for both principal kinds. */
export type TokenRow = {
  id: string;
  name: string;
  role: TokenRole | string;
  expiresAt: string | null;
  revokedAt: string | null;
  lastUsedAt: string | null;
  createdAt: string;
  /** The workspace a service identity belongs to; null for a person's token. */
  workspaceId: string | null;
};

/** The mint answer: the row's identity plus the secret, which exists only here. */
export type MintedToken = {
  id: string;
  name: string;
  role: TokenRole | string;
  expiresAt: string | null;
  secret: string;
  workspaceId: string | null;
};

function stringOrNull(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

/**
 * snake_case → camelCase, one row of the API's list.
 *
 * Null when the row could neither be rendered nor revoked — no id or no
 * name — which is a fault in the answer, not a token. The other columns are
 * display and default to null, because a row that cannot say when it
 * expires is still one the person may need to revoke.
 */
export function tokenRowFrom(raw: unknown): TokenRow | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  if (typeof r.id !== "string" || typeof r.name !== "string") return null;
  return {
    id: r.id,
    name: r.name,
    role: typeof r.role === "string" ? r.role : "unknown",
    expiresAt: stringOrNull(r.expires_at),
    revokedAt: stringOrNull(r.revoked_at),
    lastUsedAt: stringOrNull(r.last_used_at),
    createdAt: typeof r.created_at === "string" ? r.created_at : "",
    workspaceId: stringOrNull(r.workspace_id),
  };
}

/** The API's `tokens` list → rows; null when it is not a list at all. */
export function tokenRowsFrom(raw: unknown): TokenRow[] | null {
  if (!Array.isArray(raw)) return null;
  return raw.flatMap((row) => {
    const parsed = tokenRowFrom(row);
    return parsed ? [parsed] : [];
  });
}

/**
 * The API's mint answer → the minted token, or null when the secret is not
 * there. A 201 without a usable secret is a failure: the person's next act
 * is to paste it, and there would be nothing to paste.
 */
export function mintedTokenFrom(raw: unknown): MintedToken | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  if (
    typeof r.id !== "string" ||
    typeof r.name !== "string" ||
    !isTokenSecret(r.secret)
  ) {
    return null;
  }
  return {
    id: r.id,
    name: r.name,
    role: typeof r.role === "string" ? r.role : "unknown",
    expiresAt: stringOrNull(r.expires_at),
    secret: r.secret,
    workspaceId: stringOrNull(r.workspace_id),
  };
}

// ── What a row says about itself ───────────────────────────────────────────

const HOUR_MS = 3_600_000;
const DAY_MS = 86_400_000;

export type TokenRowState = "live" | "revoked" | "expired";

/**
 * Revoked beats expired beats live. An unparseable expiry reads as live,
 * deliberately: the other reading hides the Revoke control on a row whose
 * expiry the screen cannot vouch for, and revoking is the safe act.
 */
export function tokenRowState(
  row: Pick<TokenRow, "revokedAt" | "expiresAt">,
  now: Date = new Date(),
): TokenRowState {
  if (row.revokedAt) return "revoked";
  if (row.expiresAt) {
    const at = new Date(row.expiresAt).getTime();
    if (!Number.isNaN(at) && at <= now.getTime()) return "expired";
  }
  return "live";
}

/** "expires in 89 days" · "expired" · "no expiry". Rounded UP, so a token never reads as expired before it is. */
export function expiryCopy(expiresAt: string | null, now: Date): string {
  if (expiresAt === null) return "no expiry";
  const at = new Date(expiresAt).getTime();
  if (Number.isNaN(at)) return "expiry unknown";
  const remaining = at - now.getTime();
  if (remaining <= 0) return "expired";
  const days = Math.ceil(remaining / DAY_MS);
  return days === 1 ? "expires in 1 day" : `expires in ${days} days`;
}

/** "never used" · "used just now" · "used 3 hours ago" · "used yesterday" · "used 5 days ago". */
export function lastUsedCopy(lastUsedAt: string | null, now: Date): string {
  if (lastUsedAt === null) return "never used";
  const at = new Date(lastUsedAt).getTime();
  if (Number.isNaN(at)) return "last use unknown";
  const ago = now.getTime() - at;
  if (ago < HOUR_MS) return "used just now";
  if (ago < DAY_MS) {
    const hours = Math.floor(ago / HOUR_MS);
    return hours === 1 ? "used 1 hour ago" : `used ${hours} hours ago`;
  }
  const days = Math.floor(ago / DAY_MS);
  return days === 1 ? "used yesterday" : `used ${days} days ago`;
}

/** "created 2026-09-15" — the date only, locale-free, so the server and the browser agree. */
export function createdCopy(createdAt: string): string {
  const at = new Date(createdAt);
  if (Number.isNaN(at.getTime())) return "created: unknown";
  return `created ${at.toISOString().slice(0, 10)}`;
}

// ── The proxy calls ────────────────────────────────────────────────────────

export type MintTokenResult =
  | { ok: true; token: MintedToken }
  | { ok: false; error: string; status: number };

export type RevokeTokenResult =
  { ok: true } | { ok: false; error: string; status: number };

function mintResultFrom(result: BffResult): MintTokenResult {
  // RE-PROJECTED, not passed through. `callBff`'s failure arm also carries the
  // refused `body` (for `command-client.ts`, the one caller that reads a second
  // key); this function's return value is this module's PUBLIC result, and it
  // has always been exactly `{ ok, error, status }`. Widening it here would put
  // the whole refused body on a type that never declared it.
  if (!result.ok) {
    return { ok: false, error: result.error, status: result.status };
  }
  const r = result.data;
  if (
    typeof r.id !== "string" ||
    typeof r.name !== "string" ||
    typeof r.secret !== "string"
  ) {
    return { ok: false, error: "malformed_response", status: result.status };
  }
  return {
    ok: true,
    token: {
      id: r.id,
      name: r.name,
      role: typeof r.role === "string" ? r.role : "unknown",
      expiresAt: stringOrNull(r.expiresAt),
      secret: r.secret,
      workspaceId: stringOrNull(r.workspaceId),
    },
  };
}

/** A revoke the proxy did not confirm is not a success — the row may still be live. */
function revokeResultFrom(result: BffResult): RevokeTokenResult {
  if (!result.ok) {
    return { ok: false, error: result.error, status: result.status };
  }
  if (result.data.revoked !== true) {
    return { ok: false, error: "malformed_response", status: result.status };
  }
  return { ok: true };
}

export type MintMyTokenInput = {
  name: string;
  role: TokenRole;
  expiresInDays: number;
};

/** Mint a token that acts as YOU. The secret comes back once. */
export async function mintMyToken(
  input: MintMyTokenInput,
): Promise<MintTokenResult> {
  return mintResultFrom(
    await callBff(
      "/api/me/tokens",
      postJson({
        name: input.name,
        role: input.role,
        expires_in_days: input.expiresInDays,
      }),
    ),
  );
}

export async function revokeMyToken(
  tokenId: string,
): Promise<RevokeTokenResult> {
  return revokeResultFrom(
    await callBff(`/api/me/tokens/${tokenId}`, { method: "DELETE" }),
  );
}

export type MintServiceTokenInput = { name: string; expiresInDays: number };

/**
 * Mint the WORKSPACE's service identity. No role in the body, by design:
 * the API fixes `readonly` in this release, and a body that could name a
 * role would be the first place a write-capable service principal could be
 * asked for, before anything authorises one.
 */
export async function mintServiceToken(
  workspaceId: string,
  input: MintServiceTokenInput,
): Promise<MintTokenResult> {
  return mintResultFrom(
    await callBff(
      `/api/workspaces/${workspaceId}/tokens`,
      postJson({ name: input.name, expires_in_days: input.expiresInDays }),
    ),
  );
}

export async function revokeServiceToken(
  workspaceId: string,
  tokenId: string,
): Promise<RevokeTokenResult> {
  return revokeResultFrom(
    await callBff(`/api/workspaces/${workspaceId}/tokens/${tokenId}`, {
      method: "DELETE",
    }),
  );
}

// ── The sentences ──────────────────────────────────────────────────────────

/**
 * A sentence for a failed mint. Every branch says what did NOT happen.
 *
 * The 403 names the floor for a SERVICE identity, because that is the only
 * mint a session can be refused for its role: a person's own token has no
 * role check to fail.
 */
export function mintTokenRefusalCopy(reason: unknown): string {
  switch (reason) {
    case "invalid_args":
    case "invalid_name":
    case "invalid_role":
    case "invalid_expiry":
    case "malformed_body":
      return `Give the token a name of 1–${TOKEN_NAME_MAX} characters and an expiry of ${EXPIRY_DAYS_MIN}–${EXPIRY_DAYS_MAX} days. Nothing was created.`;
    case "insufficient_role":
    case "http_403":
      return "You need to be an admin or owner of this workspace to mint a service identity. Nothing was created.";
    case "malformed_response":
      // The one branch that cannot promise "nothing was created": the API
      // may have minted and this tier could not read the answer. Said so.
      return "Storydump could not read the minted token back, so it cannot be shown. If a new token appears in the list after a reload, revoke it and mint another.";
    case "unauthenticated":
    case "http_401":
      return notAuthenticatedCopy("Nothing was created.");
    case "unreachable":
    case "target_router_unreachable":
      return unreachableCopy("Nothing was created");
  }
  return "Could not mint the token. Nothing was created — try again shortly.";
}

export function revokeTokenRefusalCopy(reason: unknown): string {
  switch (reason) {
    case "not_found":
    case "not found":
    case "http_404":
      return "That token is no longer here — it may already be revoked. Reload the page.";
    case "insufficient_role":
    case "http_403":
      return "You need to be an admin or owner of this workspace to revoke a service identity. Nothing changed.";
    case "malformed_response":
      return "Storydump could not confirm the revoke. Reload the page to see whether it took; if not, try again.";
    case "unauthenticated":
    case "http_401":
      return notAuthenticatedCopy("Nothing changed.");
    case "unreachable":
    case "target_router_unreachable":
      return unreachableCopy("Nothing changed");
  }
  return "Could not revoke the token. Nothing changed — try again shortly.";
}
