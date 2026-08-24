import Link from "next/link";
import { Sparkles } from "lucide-react";

/**
 * What the dashboard shows a signed-in user who has connected nothing.
 *
 * Before web sign-up this state could not occur — every user arrived
 * pre-provisioned through a bot, so the dashboard has never had to render an
 * account with no tenant behind it.
 *
 * Two rules it follows:
 *
 *  - It reads as a product waiting for one thing, not as an error and not as a
 *    nag. There is no warning colour, no alert icon, no "you must" phrasing.
 *  - Telegram is not mentioned. It is optional, and naming it here would put an
 *    optional service in front of a person who has not asked for it.
 */
export function NoTenantEmptyState() {
  return (
    <div className="flex min-h-[60vh] items-center justify-center">
      <div className="max-w-md space-y-6 text-center">
        <div className="mx-auto flex h-16 w-16 items-center justify-center rounded-full bg-primary/10">
          <Sparkles className="h-8 w-8 text-primary" />
        </div>

        <div className="space-y-2">
          <h2 className="text-2xl font-bold tracking-tight">
            Nothing scheduled yet.
          </h2>
          <p className="text-muted-foreground">
            Connect an Instagram account and a media library, and Storydump will
            start posting Stories on your schedule.
          </p>
        </div>

        <Link
          href="/welcome"
          className="inline-flex items-center justify-center rounded-md bg-primary px-6 py-2.5 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90"
        >
          Get started
        </Link>
      </div>
    </div>
  );
}
