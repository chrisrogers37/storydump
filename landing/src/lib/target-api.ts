/**
 * The client for the target router.
 *
 * THE ONLY CLIENT. `backend.ts` was the legacy one — every call
 * `/api/onboarding/*`, authenticated by `generateUrlToken(chat_id, user_id)`,
 * a credential HMAC'd with the Telegram bot token that a user who had never
 * used Telegram could not produce. That is why it could not be widened to
 * serve web sign-up, and it is gone: the screens it served are on this
 * client now, and there is no second era to translate to.
 *
 * ── The credential ─────────────────────────────────────────────────────────
 *
 * Every call carries the session token as a bearer credential and NOTHING else.
 * The workspace a call acts on is named in the path, never in the credential —
 * so the same token serves a user with no workspace, one workspace, or six, and
 * `POST /workspaces` (the call that creates the first one) is expressible with
 * the same credential as every other call.
 *
 * That property is the requirement, not the encoding: a credential that must
 * name a tenant cannot express a signed-in user who has none, and on the
 * greenfield that is every user for their first minute. Get it wrong and the
 * first two screens of the funnel are unreachable, with no way to ever obtain
 * a first workspace.
 */

import { isPlainCode, refusalFacts, type RefusalFacts } from "./refusal-facts";

export const TARGET_API_URL =
  process.env.TARGET_API_URL || process.env.BACKEND_URL || "http://localhost:8000";

/**
 * The API mounts THREE prefixes and they are not interchangeable.
 *
 * `/api/v1` is the authenticated resource + command surface. `/auth` is the
 * sign-in plane, which is deliberately outside it because two of its endpoints
 * are PRE-authentication — there is no principal yet to scope them to.
 * `/public` serves a visitor with no account at all: the marketing waitlist.
 *
 * Callers name the plane rather than the prefix, so a path can never silently
 * be assembled against the wrong one.
 */
export type ApiPlane = "v1" | "auth" | "public";

const PREFIX: Record<ApiPlane, string> = {
  v1: "/api/v1",
  auth: "/auth",
  public: "/public",
};

export type TargetResult<T> =
  | { ok: true; data: T }
  | { ok: false; status: number; error: string; facts?: RefusalFacts };

/**
 * Call the target router with the caller's session token.
 *
 * NOT `fail_open`. A call that cannot be completed returns a typed failure and
 * the caller decides — no substituted default, no empty array standing in for a
 * failed list. An empty list and an unreachable router have opposite remedies
 * ("you have no workspaces yet" sends someone to create one; "the router is
 * down" does not), and collapsing them fails toward *everything is fine*.
 *
 * `refusalFacts` asks for a refusal's facts beside its reason, through the
 * allow-list (`refusal-facts.ts`). The command route is the one caller that
 * asks (`refusal-facts.test.ts` pins that); every other result is unchanged.
 */
export async function targetFetch<T = unknown>(
  path: string,
  sessionToken: string | null,
  init?: RequestInit & { revalidate?: number; plane?: ApiPlane; refusalFacts?: boolean },
): Promise<TargetResult<T>> {
  const headers = new Headers(init?.headers);
  headers.set("Accept", "application/json");
  if (init?.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const response = await send(path, sessionToken, headers, init);
  if (response === null) {
    // The router is not reachable. Until it is mounted this is the expected
    // state of every call here, and it must not be reported as "no data".
    return { ok: false, status: 503, error: "target_router_unreachable" };
  }

  if (!response.ok) {
    // The facts are read from a copy, so the reason is derived exactly as it
    // is for every caller that does not ask for them.
    const copy = init?.refusalFacts ? response.clone() : null;
    const error = await readError(response);
    const facts = copy ? await readFacts(copy) : null;
    return facts
      ? { ok: false, status: response.status, error, facts }
      : { ok: false, status: response.status, error };
  }

  if (response.status === 204) return { ok: true, data: undefined as T };

  try {
    return { ok: true, data: (await response.json()) as T };
  } catch {
    return { ok: false, status: response.status, error: "malformed_response" };
  }
}

/**
 * A GET whose answer is bytes, not JSON (a media thumbnail), returned as the
 * API's own response: its status, its headers and a body that streams through
 * unread. Same credential and plane as `targetFetch`. A router that cannot be
 * reached is a bodiless 503, never something a caller could relay as a picture.
 */
export async function targetFetchBytes(
  path: string,
  sessionToken: string | null,
): Promise<Response> {
  const response = await send(path, sessionToken, new Headers({ Accept: "image/*" }));
  return response ?? new Response(null, { status: 503 });
}

/**
 * One call to the router: the URL from the plane, the session token as the
 * bearer credential and nothing else (the header above), never cached. Null
 * when the router cannot be reached; each caller answers that in its own shape.
 */
async function send(
  path: string,
  sessionToken: string | null,
  headers: Headers,
  init?: RequestInit & { plane?: ApiPlane },
): Promise<Response | null> {
  if (sessionToken) headers.set("Authorization", `Bearer ${sessionToken}`);
  try {
    return await fetch(`${TARGET_API_URL}${PREFIX[init?.plane ?? "v1"]}${path}`, {
      ...init,
      headers,
      cache: "no-store",
    });
  } catch {
    return null;
  }
}

/**
 * A reason string, never the raw body.
 *
 * The body of a failed auth call is exactly where a token or a subject ends up
 * if anything upstream is careless, and this value reaches logs and error pages.
 *
 * `reason` is the API's one code carrier: its refusals are `{detail}` alone
 * (tenancy — deliberately code-less) or `{detail, reason}` (the command port
 * and invitations, `src/api/app.py`). Nothing it sends carries `error`; that
 * key is what THIS tier's route handlers emit to the browser, and it never
 * reaches here. The port's 409s are the queue's NORMAL answers — "already
 * acted on" and "use Posted myself" are different sentences, and `http_409`
 * names neither.
 */
async function readError(response: Response): Promise<string> {
  try {
    const reason = ((await response.json()) as { reason?: unknown })?.reason;
    if (isPlainCode(reason)) {
      return reason;
    }
  } catch {
    // fall through
  }
  return `http_${response.status}`;
}

/**
 * A refusal's facts, through the allow-list: never the body, and never the
 * `detail` sentence beside them. A body that is not JSON has none.
 */
async function readFacts(response: Response): Promise<RefusalFacts | null> {
  try {
    return refusalFacts(((await response.json()) as { facts?: unknown })?.facts);
  } catch {
    return null;
  }
}
