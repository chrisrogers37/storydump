/**
 * The browser's one door to this tier's own routes, and the shape every
 * caller answers in.
 *
 * ONE COPY OF THE BLOCK (#1344): `try { fetch } catch { unreachable/0 }`,
 * then `json().catch(() => ({}))`, then
 * `typeof data?.error === "string" ? data.error : http_<status>`, written
 * here once, so a change to the shape — a timeout, a non-JSON 502 body, a
 * `Retry-After` — has one place to land.
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
 * whole, not a command), and the one DELETE that carries a body is a Drive
 * folder's removal from a workspace whose mix is versioned (`drive.ts`).
 */
export function postJson(
  body: Record<string, unknown>,
  method: "POST" | "PUT" | "PATCH" | "DELETE" = "POST",
): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}
