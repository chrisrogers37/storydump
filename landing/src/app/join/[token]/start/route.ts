import { NextRequest, NextResponse } from "next/server";

/** Remembered just long enough to survive the round trip to Google. */
export const INVITE_COOKIE = "storydump_invite";

/**
 * GET /join/[token]/start — sign in, and come back here afterwards.
 *
 * The invitation token has to survive a redirect to Google and back. It is put
 * in a short-lived httpOnly cookie rather than a `?next=` parameter, for one
 * reason: a `next` parameter is a redirect target an attacker can set, and the
 * fix for that is an allowlist nobody maintains. A cookie holding only the
 * token cannot name a destination at all — this tier decides where to land, and
 * the worst a forged value can do is fail to resolve an invitation.
 *
 * SameSite=Lax deliberately, matching the OIDC state cookie: the return from
 * accounts.google.com is a top-level GET, which Strict would withhold.
 */
export async function GET(
  request: NextRequest,
  context: { params: Promise<{ token: string }> },
) {
  const { token } = await context.params;

  const response = NextResponse.redirect(
    new URL("/auth/google", request.nextUrl.origin),
  );

  if (token && token.length <= 256) {
    response.cookies.set(INVITE_COOKIE, token, {
      httpOnly: true,
      secure: process.env.NODE_ENV === "production",
      sameSite: "lax",
      path: "/",
      maxAge: 900,
    });
  }

  return response;
}
