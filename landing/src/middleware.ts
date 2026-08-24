import { NextRequest, NextResponse } from "next/server";
import { verifySessionToken, SESSION_COOKIE } from "@/lib/session";
import { webSignupEnabled, hasTenant } from "@/lib/web-signup";

export async function middleware(request: NextRequest) {
  // Server-side dev auth bypass — never expose via NEXT_PUBLIC_
  if (process.env.DEV_AUTH_BYPASS === "true" && process.env.NODE_ENV !== "production") {
    return NextResponse.next();
  }

  const token = request.cookies.get(SESSION_COOKIE)?.value;

  if (!token) {
    return NextResponse.redirect(new URL("/login", request.url));
  }

  const session = await verifySessionToken(token);
  if (!session) {
    const response = NextResponse.redirect(new URL("/login", request.url));
    response.cookies.delete(SESSION_COOKIE);
    return response;
  }

  // The gate asks "do you have a tenant?", not "do you have Telegram?". Those
  // coincided only because every tenant was born from a Telegram group.
  //
  // With web sign-up on, a signed-in user with no tenant reaches /dashboard and
  // sees an empty state there. Sending them to /instances would return them to
  // the Telegram funnel, which is the behaviour this replaces.
  //
  // With the flag off, behaviour is byte-for-byte what it was.
  if (request.nextUrl.pathname.startsWith("/dashboard") && !hasTenant(session)) {
    if (!webSignupEnabled()) {
      return NextResponse.redirect(new URL("/instances", request.url));
    }
  }

  return NextResponse.next();
}

export const config = {
  matcher: ["/dashboard/:path*", "/instances"],
};
