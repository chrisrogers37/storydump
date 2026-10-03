import { NextRequest, NextResponse } from "next/server";
import {
  SESSION_COOKIE,
  WORKSPACE_COOKIE,
  getSessionToken,
} from "@/lib/session";
import { refuseCrossSite } from "@/lib/route-guards";
import { targetFetch, type TargetResult } from "@/lib/target-api";

/** The `?signout=` notice a sign-out lands on, or null when it fully worked (see `signOut`). */
function signOutNotice(
  everywhere: boolean,
  result: TargetResult<{ revoked?: number }> | null,
): string | null {
  if (!result) return everywhere ? "stale" : null;
  // For "Sign out of all devices" the remote half IS the point: a person
  // securing a lost phone must not be told it worked when it did not.
  if (!result.ok) return everywhere ? "incomplete" : "unconfirmed";
  return everywhere && result.data.revoked === 0 ? "stale" : null;
}

/**
 * Expire the session cookie on every Domain the API may have set it with.
 *
 * The API sets `sd_session` with `Domain=SESSION_COOKIE_DOMAIN` so the front
 * end can read it, and a cookie is only replaced by one with the same Domain:
 * a host-only `cookies.delete` left the production cookie in place, so a
 * signed-out browser kept sending a token that had been revoked (or, when the
 * revocation failed, one that had not). The variable must cover this host
 * (`principal.py`'s deliverability check), so it is this host or one of its
 * parents; expiring each of them needs no copy of the setting here. A parent
 * the browser treats as a public suffix is ignored by the browser.
 */
function expireSessionCookie(response: NextResponse, url: URL) {
  const host = url.hostname;
  if (!host.includes(".") || /^[\d.]+$/.test(host)) return; // localhost, an IP
  const secure = url.protocol === "https:" ? "; Secure" : "";
  const labels = host.split(".");
  for (let i = 0; i < labels.length - 1; i++) {
    response.headers.append(
      "Set-Cookie",
      `${SESSION_COOKIE}=; Domain=${labels.slice(i).join(".")}; Path=/; Max-Age=0;` +
        ` Expires=Thu, 01 Jan 1970 00:00:00 GMT; HttpOnly; SameSite=Lax${secure}`,
    );
  }
}

/**
 * Sign out — revoke server-side, THEN clear the cookie.
 *
 * Clearing the cookie is not a logout. Under the old self-contained JWT it was
 * the only thing available, and it meant a copied token stayed valid until it
 * expired; `session_tokens.revoked_at` is the column that makes sign-out mean
 * something, and this is the only place that writes it.
 *
 * `POST /auth/signout` lives on the AUTH plane rather than under /api/v1, and
 * the API's own note says why it needs no principal: "an already-dead session
 * is not an error." Naming the plane explicitly is what stops this being
 * assembled against the wrong prefix — which it was, silently, until the
 * router landed and the path turned out not to exist.
 *
 * The cookie is cleared even when revocation fails. The alternative — refusing
 * to sign out because the router is unreachable — leaves someone signed in at a
 * shared machine because of an outage they cannot see. The local half always
 * happens; the durable half is attempted, and when it fails the person is told
 * rather than kept signed in.
 *
 * `?everywhere=1` is "Sign out of all devices" (Settings › General): the API
 * revokes every live session of this person, this one included
 * (`POST /auth/signout?everywhere=true`). The local half is the same either
 * way: this browser's cookies go too.
 *
 * When the durable half did not happen, the browser lands on
 * `/login?signout=<why>` and the page says so (`SIGNOUT_NOTICES`):
 * `unconfirmed` — the API never confirmed this session ended, so a copy of the
 * cookie may still work; `incomplete` — the other devices could not be signed
 * out; `stale` — this browser's session was already dead, so the API could not
 * tell whose devices to sign out and revoked nothing.
 */
async function signOut(request: NextRequest) {
  const refused = refuseCrossSite(request);
  if (refused) return refused;

  const token = await getSessionToken();
  const everywhere = request.nextUrl.searchParams.get("everywhere") === "1";

  const result = token
    ? await targetFetch<{ revoked?: number }>(
        everywhere ? "/signout?everywhere=true" : "/signout",
        token,
        { method: "POST", plane: "auth" },
      )
    : null;
  const notice = signOutNotice(everywhere, result);

  const response = NextResponse.redirect(
    new URL(notice ? `/login?signout=${notice}` : "/login", request.url),
  );
  response.cookies.delete(SESSION_COOKIE);
  response.cookies.delete(WORKSPACE_COOKIE);
  // After the `cookies` calls: they rewrite every Set-Cookie header.
  expireSessionCookie(response, request.nextUrl);
  return response;
}

// POST ONLY, DELIBERATELY. A GET here revoked sessions for anything that
// speculatively fetches a URL — Next's `<Link>` prefetch did exactly that from
// `/welcome`, killing every session about a second after it was minted. Sign-out
// mutates `session_tokens.revoked_at`, so it is not safe as a GET for any caller,
// not merely for the one that bit us.
export const POST = signOut;
