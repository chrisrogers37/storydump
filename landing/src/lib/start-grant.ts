import { callBff, postJson } from "./bff";

/**
 * Start an OAuth grant through one of the BFF's `…/connect` proxies and return
 * where the browser goes — the browser twin of `start-proxy.ts`'s
 * `proxyStartOfGrant`. Provider-free: the caller names the path and the ONE
 * host the authorization URL may point at, and that guard runs here, at the
 * line before `window.location.assign`. A 200 whose URL fails the guard is a
 * failure (`malformed_authorization_url`), never a navigation.
 *
 * *body* is what the start posts: `{}` for every caller but one, which is
 * the empty start. The Instagram connect sends its history choice in it
 * (`connect-choice.ts`, #1644); the guard is the same either way.
 */
export type GrantResult =
  | { ok: true; authorizationUrl: string }
  | { ok: false; error: string; status: number };

export async function requestGrant(
  path: string,
  isAllowedUrl: (value: string) => boolean,
  body: Record<string, unknown> = {},
): Promise<GrantResult> {
  const result = await callBff(path, postJson(body));
  if (!result.ok) {
    return { ok: false, error: result.error, status: result.status };
  }
  const url = result.data.authorizationUrl;
  if (typeof url !== "string" || !isAllowedUrl(url)) {
    return { ok: false, error: "malformed_authorization_url", status: result.status };
  }
  return { ok: true, authorizationUrl: url };
}
