/**
 * Start an OAuth grant through one of the BFF's `…/connect` proxies and return
 * where the browser goes — the browser twin of `start-proxy.ts`'s
 * `proxyStartOfGrant`. Provider-free: the caller names the path and the ONE
 * host the authorization URL may point at, and that guard runs here, at the
 * line before `window.location.assign`. A 200 whose URL fails the guard is a
 * failure (`malformed_authorization_url`), never a navigation.
 */
export type GrantResult =
  | { ok: true; authorizationUrl: string }
  | { ok: false; error: string; status: number };

export async function requestGrant(
  path: string,
  isAllowedUrl: (value: string) => boolean,
): Promise<GrantResult> {
  let response: Response;
  try {
    response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
  } catch {
    return { ok: false, error: "unreachable", status: 0 };
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = typeof data?.error === "string" ? data.error : `http_${response.status}`;
    return { ok: false, error, status: response.status };
  }
  const url = data?.authorizationUrl;
  if (typeof url !== "string" || !isAllowedUrl(url)) {
    return { ok: false, error: "malformed_authorization_url", status: response.status };
  }
  return { ok: true, authorizationUrl: url };
}
