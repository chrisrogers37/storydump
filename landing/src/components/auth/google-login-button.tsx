import { Button } from "@/components/ui/button";
import { googleSigninAvailable } from "@/lib/google-oidc";

/**
 * The Google sign-in button — rendered ONLY where it can actually complete.
 *
 * Google matches redirect URIs exactly and forbids wildcards, so on any origin
 * that is not the registered one the flow ends in `redirect_uri_mismatch` —
 * a failure no amount of reading our code explains. And while
 * `SUBJECT_STORAGE_AVAILABLE` is false there is nowhere to record the user even
 * if it did complete.
 *
 * So `googleSigninAvailable()` decides, and where it says no this renders
 * nothing at all rather than a disabled control: an unavailable button still
 * says "this is how you sign in", which is the wrong thing to tell someone
 * whose only working option is below it. Same rule virgil applied by leaving it
 * out entirely — a button that cannot complete is worse than no button.
 *
 * This is a plain link, not a form: `GET /auth/google` is a redirect, and a
 * fetch would follow it into accounts.google.com inside the page.
 */
export function GoogleLoginButton({ origin }: { origin: string | null }) {
  if (!googleSigninAvailable(origin)) return null;

  // The design system's outline variant via `asChild`, NOT a hand-rolled
  // near-copy of it. The copy differed by shadow-sm vs shadow-xs, a missing
  // hover:text-accent-foreground, and — the one that showed — the absent
  // focus-visible ring, so a focused Google button looked unlike every other
  // button in the app. `asChild` keeps the anchor semantics, which matter: this
  // is a redirect, not a form submission.
  return (
    <Button asChild variant="outline" className="w-full gap-2">
      <a href="/auth/google">
        <svg className="h-4 w-4" viewBox="0 0 24 24" aria-hidden="true">
          <path
            fill="#4285F4"
            d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 0 1-2.2 3.32v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.1Z"
          />
          <path
            fill="#34A853"
            d="M12 23c2.97 0 5.46-.98 7.28-2.65l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84A11 11 0 0 0 12 23Z"
          />
          <path
            fill="#FBBC05"
            d="M5.84 14.11a6.6 6.6 0 0 1 0-4.22V7.05H2.18a11 11 0 0 0 0 9.9l3.66-2.84Z"
          />
          <path
            fill="#EA4335"
            d="M12 4.75c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 1.46 14.97.5 12 .5A11 11 0 0 0 2.18 7.05l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53Z"
          />
        </svg>
        Continue with Google
      </a>
    </Button>
  );
}
