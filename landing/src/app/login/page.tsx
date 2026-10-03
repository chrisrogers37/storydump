import { Suspense } from "react";
import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { GoogleLoginButton } from "@/components/auth/google-login-button";
import { siteConfig } from "@/config/site";
import { noindexMetadata } from "@/lib/seo";
import { Wordmark } from "@/design/brand";
import { Screen } from "@/design/screen";
import { Card } from "@/components/ui/card";
import { NEW_HERE, WAITLIST_HREF } from "./content";
import { LoginNotice } from "./login-notice";

export const metadata = {
  title: "Sign in",
  description: `Sign in to ${siteConfig.name} with Google.`,
  ...noindexMetadata,
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
    <Screen>
      <Link
        href="/"
        className="inline-flex items-center gap-1.5 text-sm text-muted-foreground transition-colors hover:text-foreground"
      >
        <ArrowLeft className="h-4 w-4" />
        Back to {siteConfig.name}
      </Link>

      <div className="space-y-2 text-center">
        <h1>
          <Wordmark className="justify-center text-3xl" markClassName="size-7" />
        </h1>
        <p className="text-muted-foreground text-sm">
          Sign in to your Story queue.
        </p>
      </div>

      <Suspense fallback={null}>
        <LoginNotice />
      </Suspense>

      <Card className="p-6">
        <GoogleLoginButton />
      </Card>

      <p className="text-center text-xs text-muted-foreground">
        {NEW_HERE.lead}
        <Link
          href={WAITLIST_HREF}
          className="underline underline-offset-4 transition-colors hover:text-foreground"
        >
          {NEW_HERE.link}
        </Link>
        {NEW_HERE.tail}
      </p>
    </Screen>
  );
}
