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
  const hasSessionCookie = Boolean(request.cookies.get(SESSION_COOKIE)?.value);

  if (!hasSessionCookie) {
    return NextResponse.redirect(new URL("/login", request.url));
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

export const config = {
  matcher: ["/dashboard/:path*", "/workspaces"],
};
