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

type Reason = "expired" | "already_linked" | "email_in_use";

const CONTENT: Record<
  Reason | "unknown",
  { heading: string; body: string; primary: string; href: string; secondary?: string }
> = {
  expired: {
    heading: "That sign-in link has expired.",
    body: "Sign-in links are good for a few minutes and can only be used once. Start again and you'll be straight back in.",
    primary: "Sign in",
    href: "/login",
  },
  already_linked: {
    heading: "That account is already connected to a different Storydump user.",
    body: "A Storydump account can only be connected to one Google account, and one Telegram account. Sign in as that user, or disconnect it there first.",
    primary: "Sign in",
    href: "/login",
    secondary: "Contact support",
  },
  email_in_use: {
    heading: "We can't sign you in with that email.",
    // Says WHY, not just what. The reason is a real security property, and
    // stating it turns a dead end into something a person can accept.
    body: "Another Storydump account already uses this email. We don't merge accounts automatically, because that would let anyone who obtains an email address take over the account behind it.",
    primary: "Try a different account",
    href: "/login",
    secondary: "Contact support",
  },
  unknown: {
    heading: "Something went wrong signing you in.",
    body: "The sign-in didn't complete. Try again, and if it keeps happening let us know.",
    primary: "Sign in",
    href: "/login",
  },
};

function resolve(reason: string | undefined): keyof typeof CONTENT {
  if (reason === "expired" || reason === "already_linked" || reason === "email_in_use") {
    return reason;
  }
  return "unknown";
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
