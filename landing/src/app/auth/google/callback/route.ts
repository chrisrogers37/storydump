import { NextRequest, NextResponse } from "next/server";
import {
  STATE_COOKIE,
  exchangeAndVerify,
  googleSigninAvailable,
  readStateToken,
} from "@/lib/google-oidc";
import { SESSION_COOKIE, SESSION_COOKIE_OPTIONS } from "@/lib/session";
import { targetFetch } from "@/lib/target-api";

/**
 * GET /auth/google/callback — verify, then sign in (#1015).
 *
 * Everything up to a cryptographically verified OIDC subject happens here; the
 * write happens in the target router, which owns the database. This tier never
 * holds a connection, which is why the subject is forwarded rather than stored.
 *
 * IDENTITY KEYS ON `sub`, NEVER ON EMAIL. `user_identities` is UNIQUE on
 * (provider, external_id) and that is deliberate: a Google account can change
 * its address, and two accounts can present the same unverified one. Matching
 * on email would let a changed address orphan a user from their workspaces, and
 * an unverified one hand a stranger somebody else's account.
 *
 * The redirect is always /welcome. It is the one place that decides where a
 * signed-in user belongs — a returning user with workspaces is forwarded on
 * from there — so this route does not need to ask, and there is one answer to
 * keep correct instead of two.
 */
export async function GET(request: NextRequest) {
  if (!googleSigninAvailable(request.nextUrl.origin)) {
    return new NextResponse(null, { status: 404 });
  }

  const url = request.nextUrl;
  const clear = (res: NextResponse) => {
    // One use, whatever the outcome. A state left behind is a state that can be
    // replayed against a second code.
    res.cookies.delete(STATE_COOKIE);
    return res;
  };

  // Google reports user-side refusal here rather than by failing the redirect.
  const providerError = url.searchParams.get("error");
  if (providerError) {
    return clear(NextResponse.redirect(new URL("/login", url.origin)));
  }

  const code = url.searchParams.get("code");
  const returnedState = url.searchParams.get("state");
  const stored = await readStateToken(request.cookies.get(STATE_COOKIE)?.value);

  // An absent, unverifiable or expired cookie is the same outcome as a mismatch:
  // this browser cannot show it started the flow.
  if (!code || !returnedState || !stored || returnedState !== stored.state) {
    return clear(
      NextResponse.redirect(new URL("/auth/error?reason=expired", url.origin)),
    );
  }

  let subject;
  try {
    subject = await exchangeAndVerify(code, stored.nonce);
  } catch {
    // Deliberately not surfaced to the caller: the reasons here separate a bad
    // nonce from a bad signature from a failed exchange, which is a probe's
    // question. Server logs keep it.
    return clear(NextResponse.redirect(new URL("/auth/error", url.origin)));
  }

  // The subject is verified. Everything from here is the target router's.
  //
  // `subject.sub` is deliberately NOT logged: an OIDC subject is a stable
  // personal identifier, and a log line is durable storage in the one place
  // nobody is looking after it.
  const signin = await targetFetch<{ session_token: string }>(
    "/auth/google/signin",
    null,
    {
      method: "POST",
      body: JSON.stringify({
        provider: "google",
        external_id: subject.sub,
        email: subject.emailVerified ? subject.email : undefined,
        display_name: subject.name,
      }),
    },
  );

  if (!signin.ok) {
    // Distinguishable on purpose. `unavailable` says the router could not be
    // reached, which is the expected state until it is mounted and is nothing
    // the visitor did; anything else is a refusal. Collapsing the two would
    // tell a user to try again when trying again cannot work.
    const reason =
      signin.status === 503 ? "unavailable" : "signin_failed";
    return clear(
      NextResponse.redirect(new URL(`/auth/error?reason=${reason}`, url.origin)),
    );
  }

  const response = clear(NextResponse.redirect(new URL("/welcome", url.origin)));
  response.cookies.set(
    SESSION_COOKIE,
    signin.data.session_token,
    SESSION_COOKIE_OPTIONS,
  );
  return response;
}
