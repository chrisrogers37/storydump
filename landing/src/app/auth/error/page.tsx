import Link from "next/link";
import { siteConfig } from "@/config/site";

export const metadata = {
  title: `Sign-in problem — ${siteConfig.name}`,
};

/**
 * The three sign-in failure states, which are not interchangeable.
 *
 * Each is specified separately by the security model and a person hitting the
 * third will otherwise believe the product has lost their account. Purely
 * presentational — it renders a reason, it does not decide one.
 *
 * An unrecognised reason falls back to the generic shape rather than a blank
 * page or a raw code.
 */

/**
 * The API's closed reason vocabulary, verbatim from `src/api/routes/auth.py`.
 *
 * REPLACED, and the mismatch is worth recording rather than quietly fixing:
 * this page previously rendered `expired | already_linked | email_in_use`,
 * which the API never sends. It sends these five. Zero overlap — so every real
 * sign-in failure fell through to the generic fallback, and the page LOOKED
 * like it was handling errors while handling none of them. The API's docstring
 * says "virgil's P3 already renders it", which was true of the page and not of
 * the reasons.
 *
 * These stay in step by being copied from one closed list. If the API adds a
 * sixth, it lands here as the fallback rather than as a blank — which is the
 * right failure direction, but it is a fallback, not coverage.
 */
type Reason =
  | "denied"
  | "missing_params"
  | "state_refused"
  | "exchange_failed"
  | "identity_collision";

type Content = {
  heading: string;
  body: string;
  /** Where the primary action goes. Every reason here is recoverable by
   *  starting again, so it is /login unless a reason says otherwise. */
  href: string;
  primary: string;
  /** Rendered only where a person genuinely cannot self-serve. */
  secondary?: string;
};

const CONTENT: Record<Reason | "generic", Content> = {
  denied: {
    heading: "Sign-in was cancelled.",
    body: "You closed the Google window or declined the request. Nothing was created and nothing was shared.",
    href: "/login",
    primary: "Try again",
  },
  missing_params: {
    heading: "That sign-in link was incomplete.",
    body: "It looks like the address was cut short somewhere. Start again from the sign-in page.",
    href: "/login",
    primary: "Back to sign-in",
  },
  state_refused: {
    heading: "That sign-in attempt has expired.",
    body: "A sign-in has to finish in one go, from the browser that started it. Start again and it should work.",
    href: "/login",
    primary: "Start again",
  },
  exchange_failed: {
    heading: "We could not finish signing you in.",
    body: "Google accepted you but the last step did not complete. This one is on us — try again in a moment.",
    href: "/login",
    primary: "Try again",
  },
  identity_collision: {
    // Says WHY, not just what. The reason is a real property of the account
    // model — the API never merges two identities onto one email (D35) — and a
    // person who is told "already in use" without that will keep retrying.
    heading: "That email already belongs to another account.",
    body: "Accounts are never merged, so this address cannot be added to a second one. Sign in with the account that already uses it.",
    href: "/login",
    primary: "Sign in with that account",
    secondary: "Contact us if you think this is wrong",
  },
  generic: {
    heading: "We could not sign you in.",
    body: "Something went wrong on the way back from Google. Start again from the sign-in page.",
    href: "/login",
    primary: "Back to sign-in",
    secondary: "Contact us if this keeps happening",
  },
};

function resolve(reason: string | undefined): keyof typeof CONTENT {
  return reason && reason in CONTENT && reason !== "generic"
    ? (reason as Reason)
    : "generic";
}

export default async function AuthErrorPage({
  searchParams,
}: {
  searchParams: Promise<{ reason?: string }>;
}) {
  const { reason } = await searchParams;
  const content = CONTENT[resolve(reason)];

  return (
    <div className="flex min-h-svh flex-col items-center justify-center bg-background px-4">
      <div className="w-full max-w-sm space-y-6 text-center">
        <h1 className="text-2xl font-bold tracking-tight">{content.heading}</h1>
        <p className="text-sm text-muted-foreground">{content.body}</p>

        <Link
          href={content.href}
          className="inline-flex items-center justify-center rounded-md bg-primary px-6 py-2.5 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90"
        >
          {content.primary}
        </Link>

        {content.secondary && (
          <div>
            <a
              href={`mailto:${siteConfig.contact.email}`}
              className="text-sm text-muted-foreground underline underline-offset-4 transition-colors hover:text-foreground"
            >
              {content.secondary}
            </a>
          </div>
        )}
      </div>
    </div>
  );
}
