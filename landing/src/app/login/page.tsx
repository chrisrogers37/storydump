import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { TelegramLoginButton } from "@/components/auth/telegram-login-button";
import { GoogleLoginButton } from "@/components/auth/google-login-button";
import { headers } from "next/headers";
import { webSignupEnabled } from "@/lib/web-signup";
import { siteConfig } from "@/config/site";

export const metadata = {
  title: `Login — ${siteConfig.name}`,
};

export default async function LoginPage() {
  const webSignup = webSignupEnabled();
  // The origin decides whether Google sign-in can complete here at all — see
  // GoogleLoginButton. Read from the request rather than configured, because
  // the question is "where is this page being served", not "where should it be".
  const h = await headers();
  const host = h.get("host");
  const proto = h.get("x-forwarded-proto") ?? "https";
  const origin = host ? `${proto}://${host}` : null;

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
            {webSignup
              ? "Sign in to access your dashboard."
              : "Sign in with your Telegram account to access the dashboard."}
          </p>
        </div>

        {/* space-y-3: with two sign-in controls the card has no gap between them
            and no separator, so a full-bleed Google button butts straight against
            the Telegram widget's 56px block. Harmless with one child. */}
        <div className="space-y-3 rounded-lg border bg-card p-6 shadow-sm">
          <TelegramLoginButton />
          {webSignup && <GoogleLoginButton origin={origin} />}
        </div>

        {/*
          The authorization claim below is deleted rather than reworded when web
          sign-up is on: "only users with an active Storydump bot" stops being
          true, because having a bot ceases to be a precondition for access.

          The Google button is NOT rendered here. It needs GET /auth/google,
          which is not built and is not this PR's to build — a button that 404s
          is worse than no button. See src/lib/web-signup.ts, SEAM 1.
        */}
        {!webSignup && (
          <p className="text-center text-xs text-muted-foreground">
            Only users with an active Storydump bot can access the dashboard.
          </p>
        )}
      </div>
    </div>
  );
}
