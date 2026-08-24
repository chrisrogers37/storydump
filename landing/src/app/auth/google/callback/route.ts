import { NextRequest, NextResponse } from "next/server";
import {
  STATE_COOKIE,
  SUBJECT_STORAGE_AVAILABLE,
  exchangeAndVerify,
  googleSigninAvailable,
  readStateToken,
} from "@/lib/google-oidc";
import { webSignupEnabled } from "@/lib/web-signup";

/**
 * GET /auth/google/callback — verify, then STOP at the storage boundary (#1015).
 *
 * Everything up to and including a cryptographically verified OIDC subject is
 * real. What is missing is somewhere to put it: `user_identities` is migration
 * 053 and unadopted, `users` has no subject column, and `users.telegram_user_id`
 * is still NOT NULL so a Google-only row cannot be inserted. See
 * `SUBJECT_STORAGE_AVAILABLE`.
 *
 * So this refuses at the write, with a distinct status and reason, rather than
 * minting a session that names nobody. 501 rather than 500: nothing failed, the
 * step is not implemented — and rather than 200-with-an-error, because a caller
 * that cannot distinguish "signed in" from "did not" is the failure this whole
 * surface exists to avoid.
 *
 * The refusal is not reachable through the UI: the button does not render while
 * the boundary stands, for the same reason virgil left it out entirely — a
 * button that cannot complete is worse than no button.
 */
export async function GET(request: NextRequest) {
  if (!webSignupEnabled() || !googleSigninAvailable(request.nextUrl.origin)) {
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

  // ── THE BOUNDARY ─────────────────────────────────────────────────────────
  // `subject.sub` is verified and correct at this point. There is nowhere to
  // write it. Deliberately NOT logged — an OIDC subject is a stable personal
  // identifier, and writing it to logs is the durable storage we just said we
  // do not have, in the one place nobody is looking after it.
  if (!SUBJECT_STORAGE_AVAILABLE) {
    return clear(
      NextResponse.json(
        {
          error: "sign_in_incomplete",
          detail:
            "Google verified this account, but Storydump cannot yet record it. " +
            "Awaiting the identity schema change (#1015).",
        },
        { status: 501 },
      ),
    );
  }

  // Unreachable while the boundary stands. The next PR replaces this with
  // find-or-create on the verified subject, then mints the session — and
  // mints NO tenant: that is an explicit act at the provisioning door.
  throw new Error(
    "unreachable: subject storage reported available but no writer exists",
  );
}
