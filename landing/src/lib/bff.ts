/**
 * The browser's one door to this tier's own routes, and the shape every
 * caller answers in.
 *
 * NINE HAND-WRITTEN COPIES (#1344). `tokens.ts`, `start-grant.ts`,
 * `command-client.ts`, `drive.ts` (×3), `telegram-link.ts` (×2) and
 * `category-mix.ts` each carried this block: `try { fetch } catch {
 * unreachable/0 }`, then `json().catch(() => ({}))`, then
 * `typeof data?.error === "string" ? data.error : http_<status>`. Four
 * components carried rawer variants. They agreed by luck. A change to the
 * shape — a timeout, a non-JSON 502 body, a `Retry-After` — had nine places
 * to be missed.
 *
 * WHAT STAYS WITH THE CALLER, deliberately: everything after `ok`. A Drive
 * folder list that is not an array, a Telegram link that is not a `t.me`
 * URL, an authorization URL pointing at the wrong host — each of those is a
 * 200 that is still a failure, and each is a different question. This door
 * answers "did the call happen and did the route refuse it", nothing more.
 *
 * THE ERROR STRING IS A CONTRACT. `error` is whatever the route put in
 * `{"error": …}`, verbatim, or `http_<status>` when it put nothing there —
 * and `unreachable` with status 0 when `fetch` itself threw. Every refusal
 * table on this tier (`refusalCopy`, `settingsRefusalCopy`,
 * `createWorkspaceRefusalCopy`, `destinationConnectRefusalCopy`,
 * `driveRefusalCopy`) switches on those strings. Do not "improve" them here.
 */
export type BffResult =
  | { ok: true; status: number; data: Record<string, unknown> }
  | { ok: false; error: string; status: number; body: Record<string, unknown> };

/** One call to this tier's proxy: a typed result, never a throw. */
export async function callBff(
  path: string,
  init?: RequestInit,
): Promise<BffResult> {
  let response: Response;
  try {
    response = await fetch(path, init);
  } catch {
    return { ok: false, error: "unreachable", status: 0, body: {} };
  }
  const data: unknown = await response.json().catch(() => ({}));
  const body =
    data && typeof data === "object" ? (data as Record<string, unknown>) : {};
  if (!response.ok) {
    const error =
      typeof body.error === "string" ? body.error : `http_${response.status}`;
    return { ok: false, error, status: response.status, body };
  }
  return { ok: true, status: response.status, data: body };
}

/**
 * A JSON request body. `method` defaults to POST because eight of the nine
 * callers post; `category-mix` is the PUT (a mix is a resource replaced
 * whole, not a command).
 */
export function postJson(
  body: Record<string, unknown>,
  method: "POST" | "PUT" | "PATCH" = "POST",
): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}
