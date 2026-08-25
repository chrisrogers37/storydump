import { TARGET_API_URL } from "@/lib/target-api";

/**
 * Sign in with Google.
 *
 * ── It is a LINK to the API, not a flow this tier runs ─────────────────────
 *
 * An earlier version of this front end implemented the whole OIDC exchange in
 * the BFF. The API implements it too, and the API is right: minting a session
 * means writing a `session_tokens` row, and this tier holds no database
 * connection. The API's own reasoning is the one that settles it — "no secret
 * anywhere that could mint a session for an arbitrary user — the reason this
 * lives here and not on the front end."
 *
 * So the duplicate is deleted rather than kept behind a preference. What is
 * left is one anchor to `GET /auth/google`, which redirects to Google,
 * verifies, sets the session cookie, and sends the browser back to /welcome.
 *
 * ── Why there is no availability check any more ────────────────────────────
 *
 * The previous button asked `googleSigninAvailable()` — client id, secret, and
 * an origin Google will redirect back to — and rendered null when any was
 * missing. That question now belongs entirely to the API: it holds the
 * credentials, and this tier cannot see them. Asking here would mean keeping a
 * second copy of the API's configuration, which is a fork that goes stale
 * silently and answers confidently while it does.
 *
 * A misconfigured API therefore surfaces as the API's own error rather than as
 * a missing button. That is a deliberate trade: a button that fails loudly at
 * the owner of the problem beats a button that vanishes for a reason this tier
 * inferred.
 */
export function GoogleLoginButton() {
  return (
    <a
      href={`${TARGET_API_URL}/auth/google`}
      className="inline-flex w-full items-center justify-center gap-2 rounded-md border bg-background px-4 py-2.5 text-sm font-medium transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      <GoogleMark />
      Continue with Google
    </a>
  );
}

/** Google's mark, inline so the button has no network dependency to render. */
function GoogleMark() {
  return (
    <svg className="h-4 w-4" viewBox="0 0 24 24" aria-hidden focusable="false">
      <path fill="#4285F4" d="M23.5 12.3c0-.8-.1-1.6-.2-2.3H12v4.5h6.5a5.6 5.6 0 0 1-2.4 3.7v3h3.9c2.3-2.1 3.5-5.2 3.5-8.9z" />
      <path fill="#34A853" d="M12 24c3.2 0 5.9-1.1 7.9-2.9l-3.9-3c-1.1.7-2.5 1.1-4 1.1-3.1 0-5.7-2.1-6.6-4.9H1.4v3.1A12 12 0 0 0 12 24z" />
      <path fill="#FBBC05" d="M5.4 14.3a7.2 7.2 0 0 1 0-4.6V6.6H1.4a12 12 0 0 0 0 10.8l4-3.1z" />
      <path fill="#EA4335" d="M12 4.8c1.8 0 3.3.6 4.5 1.8l3.4-3.4A12 12 0 0 0 1.4 6.6l4 3.1C6.3 6.9 8.9 4.8 12 4.8z" />
    </svg>
  );
}
