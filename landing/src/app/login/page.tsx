import Link from "next/link";
import { headers } from "next/headers";
import { ArrowLeft } from "lucide-react";
import { GoogleLoginButton } from "@/components/auth/google-login-button";
import { googleSigninAvailable } from "@/lib/google-oidc";
import { siteConfig } from "@/config/site";

export const metadata = {
  title: `Sign in — ${siteConfig.name}`,
};

/**
 * Sign in. One way in.
 *
 * The Telegram login widget is gone rather than hidden. It signed a credential
 * with the bot token, which is what made the whole tier Telegram-rooted, and
 * keeping it as a second option would have kept that root alive underneath a
 * new button. There is no configuration of this page that brings it back.
 *
 * Telegram is not gone from the product — it is a channel you bind to a
 * workspace, and an identity you can link once signed in. It is no longer a way
 * to bootstrap an account, because an account is now a `users` row that has no
 * Telegram column to bootstrap from.
 *
 * ONE CONTROL, DELIBERATELY. There is nothing to compare and nothing to choose
 * between, so the card holds a single full-width button, no separator, no "or",
 * and no second-choice styling. A chooser with one option is a chooser that
 * teaches the reader to look for the other one.
 *
 * AND THE EMPTY CARD IS HANDLED HERE, which it did not have to be before.
 * `GoogleLoginButton` renders null on an origin Google will not redirect back
 * to — correct for the button, and survivable while the Telegram widget sat
 * next to it. With one control, null leaves a bordered card containing nothing:
 * a dead end that tells the visitor neither what is wrong nor what to do. So
 * the page asks the same question the button does and says something either way.
 */
export default async function LoginPage() {
  // Read from the request rather than from config: the origin decides whether
  // Google will accept the redirect back, and a preview deployment has a
  // different one every branch.
  const h = await headers();
  const host = h.get("x-forwarded-host") ?? h.get("host");
  const proto = h.get("x-forwarded-proto") ?? "https";
  const origin = host ? `${proto}://${host}` : null;
  const canSignIn = googleSigninAvailable(origin);

  return (
    <div className="flex min-h-svh flex-col items-center justify-center bg-background px-4">
      <div className="w-full max-w-sm space-y-6">
        <Link
          href="/"
          className="inline-flex items-center gap-1.5 text-sm text-muted-foreground transition-colors hover:text-foreground"
        >
          <ArrowLeft className="h-4 w-4" />
          Back to {siteConfig.name}
        </Link>

        <div className="space-y-2 text-center">
          <h1 className="text-2xl font-bold tracking-tight">
            {siteConfig.name}
          </h1>
          <p className="text-muted-foreground text-sm">
            Sign in to access your dashboard.
          </p>
        </div>

        <div className="rounded-lg border bg-card p-6 shadow-sm">
          {canSignIn ? (
            <GoogleLoginButton origin={origin} />
          ) : (
            <p className="text-center text-sm text-muted-foreground">
              Sign-in is not available on this deployment.
            </p>
          )}
        </div>

        {canSignIn && (
          <p className="text-center text-xs text-muted-foreground">
            New here? Signing in creates your account.
          </p>
        )}
      </div>
    </div>
  );
}
