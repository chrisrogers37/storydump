import { NextRequest, NextResponse } from "next/server";
import {
  SESSION_COOKIE,
  WORKSPACE_COOKIE,
  getSessionToken,
} from "@/lib/session";
import { refuseCrossSite } from "@/lib/route-guards";
import { targetFetch } from "@/lib/target-api";

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
 * happens; the durable half is attempted and its failure is not the user's to
 * resolve.
 *
 * `?everywhere=1` is "Sign out of all devices" (Settings › General): the API
 * revokes every live session of this person, this one included
 * (`POST /auth/signout?everywhere=true`). The local half is the same either
 * way: this browser's cookies go too. When the API could not revoke the other
 * sessions, the browser lands on `/login?signout=incomplete`, which says so.
 */
async function signOut(request: NextRequest) {
  const refused = refuseCrossSite(request);
  if (refused) return refused;

  const token = await getSessionToken();
  const everywhere = request.nextUrl.searchParams.get("everywhere") === "1";

  let incomplete = false;
  if (token) {
    const result = await targetFetch(
      everywhere ? "/signout?everywhere=true" : "/signout",
      token,
      { method: "POST", plane: "auth" },
    );
    // For "Sign out of all devices" the remote half IS the point: a person
    // securing a lost phone must not be told it worked when it did not.
    incomplete = everywhere && !result.ok;
  }

  const response = NextResponse.redirect(
    new URL(incomplete ? "/login?signout=incomplete" : "/login", request.url),
  );
  response.cookies.delete(SESSION_COOKIE);
  response.cookies.delete(WORKSPACE_COOKIE);
  return response;
}

// POST ONLY, DELIBERATELY. A GET here revoked sessions for anything that
// speculatively fetches a URL — Next's `<Link>` prefetch did exactly that from
// `/welcome`, killing every session about a second after it was minted. Sign-out
// mutates `session_tokens.revoked_at`, so it is not safe as a GET for any caller,
// not merely for the one that bit us.
export const POST = signOut;
