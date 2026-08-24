import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { TelegramLoginButton } from "@/components/auth/telegram-login-button";
import { webSignupEnabled } from "@/lib/web-signup";
import { siteConfig } from "@/config/site";

export const metadata = {
  title: `Login — ${siteConfig.name}`,
};

export default function LoginPage() {
  const webSignup = webSignupEnabled();

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
          <h1 className="text-2xl font-bold tracking-tight">{siteConfig.name}</h1>
          <p className="text-muted-foreground text-sm">
            {webSignup
              ? "Sign in to access your dashboard."
              : "Sign in with your Telegram account to access the dashboard."}
          </p>
        </div>

        <div className="rounded-lg border bg-card p-6 shadow-sm">
          <TelegramLoginButton />
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
