import { NextRequest, NextResponse } from "next/server";
import {
  beginAuth,
  STATE_COOKIE,
  googleSigninAvailable,
} from "@/lib/google-oidc";
import { webSignupEnabled } from "@/lib/web-signup";

/**
 * GET /auth/google — the sign-in entry point (#1015).
 *
 * Redirects to Google with a fresh state and nonce, both also written to a
 * short-lived httpOnly cookie the callback checks. Mints nothing: no user, no
 * session, no tenant.
 *
 * 404, NOT 403, WHEN UNAVAILABLE. A route that answers 403 tells an
 * unauthenticated prober that Google sign-in exists here and is switched off;
 * 404 is what a route that is not part of this deployment looks like, which is
 * true while the flag is off or the storage boundary stands.
 */
export async function GET(request: NextRequest) {
  if (!webSignupEnabled() || !googleSigninAvailable(request.nextUrl.origin)) {
    return new NextResponse(null, { status: 404 });
  }

  const { authUrl, stateToken } = await beginAuth();

  const response = NextResponse.redirect(authUrl);
  response.cookies.set(STATE_COOKIE, stateToken, {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    // Lax, not Strict: the callback arrives as a top-level GET redirected from
    // accounts.google.com, and Strict would withhold the cookie on exactly that
    // navigation — the flow would fail every time with a state mismatch.
    sameSite: "lax",
    path: "/",
    maxAge: 600,
  });
  return response;
}
