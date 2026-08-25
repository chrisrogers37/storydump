import { NextRequest, NextResponse } from "next/server";
import { SESSION_COOKIE, WORKSPACE_COOKIE, isWorkspaceId } from "@/lib/session";

/**
 * The route gate.
 *
 * ── What it deliberately does NOT do: resolve the session ───────────────────
 *
 * The session token is opaque, so the only way to learn who holds it is to ask
 * the target router. Doing that here would put a network call in front of every
 * gated request, and middleware runs before the cache.
 *
 * So this is a PRESENCE gate, and the distinction matters: it tells an
 * anonymous visitor from a signed-in one, and nothing more. It is not an
 * authorization check and no route may rely on it as one. Every target call
 * authorizes server-side against `workspace_members`, which is where the real
 * answer has to live anyway — a forged cookie gets a 403 from the router, not
 * access, and it never got access from this file either.
 *
 * A revoked or expired token therefore reaches the page, where `getSession()`
 * resolves it to null and the page redirects. One wasted render for a signed-out
 * user, versus a round trip on every request for everybody. Revisit when there
 * is traffic to measure; there is none today.
 *
 * ── The workspace redirect ─────────────────────────────────────────────────
 *
 * Kept from the previous gate, and its reasoning is unchanged: this has to
 * happen in middleware rather than in the dashboard layout, because Next.js
 * renders layout and page segments in PARALLEL. A layout that returns without
 * rendering {children} hides the output while the page still runs its fetches.
 * Measured on a warm route — one request, one backend call, with the layout
 * guard already in place. Redirecting before render is what keeps a
 * workspace-less session from reaching a workspace-scoped fetch at all.
 */
export function middleware(request: NextRequest) {
  const cookie = request.cookies.get(SESSION_COOKIE)?.value;

  if (!cookie) {
    return NextResponse.redirect(new URL("/login", request.url));
  }

  // A legacy JWT is CLEARED, not just ignored.
  //
  // Every browser signed in before the cutover holds one, and it cannot be a
  // valid session under any circumstance — the session is now an opaque value
  // resolved server-side, so a self-contained JWT is not stale, it is a token
  // from a scheme that no longer exists. Without this it would sit in the
  // browser being re-sent and re-rejected on every request forever.
  //
  // This RECOGNISES A FORMAT; it does not validate anything, and the
  // distinction is the whole safety argument. Resolution needs a call this file
  // deliberately does not make, so the test has to be something that can be
  // wrong only in the harmless direction: it fires solely on a value that is
  // positively JWT-shaped — `eyJ` (base64url of `{"`, the opening of every JWT
  // header) plus exactly two dots. A random opaque token does not take that
  // shape, and anything that does was a JWT.
  //
  // A stale OPAQUE token is deliberately NOT cleared here. There is no local
  // test that distinguishes one from a live token, and guessing would sign out
  // valid users to tidy up invalid ones.
  if (isLegacyJwt(cookie)) {
    const response = NextResponse.redirect(new URL("/login", request.url));
    response.cookies.delete(SESSION_COOKIE);
    response.cookies.delete(WORKSPACE_COOKIE);
    return response;
  }

  const { pathname } = request.nextUrl;

  if (pathname.startsWith("/dashboard")) {
    const workspace = request.cookies.get(WORKSPACE_COOKIE)?.value;

    // No workspace selected — the dashboard has nothing to be about. /welcome
    // is where a first one gets made; it is NOT a Telegram funnel, which is
    // what the route this replaces used to send people back into.
    if (!isWorkspaceId(workspace)) {
      return NextResponse.redirect(new URL("/welcome", request.url));
    }
  }

  return NextResponse.next();
}

/** Positively JWT-shaped: `header.payload.signature`, header starting `{"`. */
export function isLegacyJwt(value: string): boolean {
  return value.startsWith("eyJ") && value.split(".").length === 3;
}

export const config = {
  matcher: ["/dashboard/:path*", "/workspaces"],
};
