/**
 * The client for the target router.
 *
 * `backend.ts` is the legacy client: every call it makes is
 * `/api/onboarding/*` authenticated by `generateUrlToken(chat_id, user_id)`, a
 * credential HMAC'd with the Telegram bot token. A user who never used Telegram
 * cannot produce one, which is why it cannot be extended to serve web sign-up
 * and is being replaced rather than widened.
 *
 * The two are not a dual path and nothing here translates between them. They are
 * one client per era: `backend.ts` serves only the dashboard screens (P6–P9),
 * which are held in this PR, and dies with them in the follow-up that ports
 * those screens onto the target router.
 *
 * ── The credential (owner: alex, #1015 D-A) ────────────────────────────────
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

export const TARGET_API_URL =
  process.env.TARGET_API_URL || process.env.BACKEND_URL || "http://localhost:8000";

/** Every target route sits under this prefix. */
const API_PREFIX = "/api/v1";

export type TargetResult<T> =
  | { ok: true; data: T }
  | { ok: false; status: number; error: string };

/**
 * Call the target router with the caller's session token.
 *
 * NOT `fail_open`. A call that cannot be completed returns a typed failure and
 * the caller decides — no substituted default, no empty array standing in for a
 * failed list. An empty list and an unreachable router have opposite remedies
 * ("you have no workspaces yet" sends someone to create one; "the router is
 * down" does not), and collapsing them fails toward *everything is fine*.
 */
export async function targetFetch<T = unknown>(
  path: string,
  sessionToken: string | null,
  init?: RequestInit & { revalidate?: number },
): Promise<TargetResult<T>> {
  const headers = new Headers(init?.headers);
  headers.set("Accept", "application/json");
  if (sessionToken) headers.set("Authorization", `Bearer ${sessionToken}`);
  if (init?.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  let response: Response;
  try {
    response = await fetch(`${TARGET_API_URL}${API_PREFIX}${path}`, {
      ...init,
      headers,
      cache: "no-store",
    });
  } catch {
    // The router is not reachable. Until it is mounted this is the expected
    // state of every call here, and it must not be reported as "no data".
    return { ok: false, status: 503, error: "target_router_unreachable" };
  }

  if (!response.ok) {
    return {
      ok: false,
      status: response.status,
      error: await readError(response),
    };
  }

  if (response.status === 204) return { ok: true, data: undefined as T };

  try {
    return { ok: true, data: (await response.json()) as T };
  } catch {
    return { ok: false, status: response.status, error: "malformed_response" };
  }
}

/**
 * A reason string, never the raw body.
 *
 * The body of a failed auth call is exactly where a token or a subject ends up
 * if anything upstream is careless, and this value reaches logs and error pages.
 */
async function readError(response: Response): Promise<string> {
  try {
    const body = await response.json();
    const reason = (body as { error?: unknown })?.error;
    if (typeof reason === "string" && /^[a-z0-9_]{1,64}$/.test(reason)) {
      return reason;
    }
  } catch {
    // fall through
  }
  return `http_${response.status}`;
}
