import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { GoogleLoginButton } from "@/components/auth/google-login-button";
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
 * The card can no longer be empty, so the guard that handled that is gone with
 * it: the button is now an unconditional link to the API's sign-in endpoint
 * rather than something that renders null when this tier cannot see Google's
 * credentials. See GoogleLoginButton for why that question moved.
 */
export default function LoginPage() {
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
          <GoogleLoginButton />
        </div>

        <p className="text-center text-xs text-muted-foreground">
          New here? Signing in creates your account.
        </p>
      </div>
    </div>
  );
}
