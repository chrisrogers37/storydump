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

    // A tenant-less session is admitted to /dashboard only. Every deeper route
    // is redirected there.
    //
    // This has to happen in middleware, and that is measured rather than
    // assumed: a guard in the dashboard layout does NOT prevent the child page
    // from running. Next.js renders layout and page segments in parallel, so a
    // layout that returns without rendering {children} hides the output while
    // the page still executes its fetches. Measured on a warm route — one
    // request, one backend call, with the layout guard already in place.
    //
    // That call is the one that matters. Those pages fetch with
    // session.activeChatId, null here; get_by_chat_id(None) compiles to
    // IS NULL and .first() carries no ORDER BY. Today no NULL-chat row can
    // exist (chat_settings.telegram_chat_id is NOT NULL, migration 006), so it
    // returns None and require_by_chat_id refuses typed. Relax that column and
    // the same call returns an arbitrary tenant-less row instead — and
    // require_by_chat_id only tests whether the RESULT is None, so it will not
    // raise. Redirecting before render is what keeps that unreachable from here.
    if (request.nextUrl.pathname !== "/dashboard") {
      return NextResponse.redirect(new URL("/dashboard", request.url));
    }
  }

  return NextResponse.next();
}

export const config = {
  matcher: ["/dashboard/:path*", "/instances"],
};
