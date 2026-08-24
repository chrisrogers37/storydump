import { redirect } from "next/navigation";
import Link from "next/link";
import { Instagram, FolderOpen, Send, ArrowRight } from "lucide-react";
import { getSession } from "@/lib/session";
import { webSignupEnabled, hasTenant } from "@/lib/web-signup";
import { siteConfig } from "@/config/site";

export const metadata = {
  title: `Welcome — ${siteConfig.name}`,
};

/**
 * First-run greeting for a signed-in user with no tenant.
 *
 * This state has never existed in the product: every user until now arrived
 * pre-provisioned through a bot, so nothing has had to greet someone with
 * nothing connected.
 *
 * It deliberately does NOT ask the user to create a workspace. Tenant minting
 * is lazy — sign-up creates a user and nothing else, and the tenant is minted
 * at the first tenant-scoped action through a provisioning door. So this screen
 * orients and hands off; it does not provision.
 *
 * Telegram appears here as one optional connected service among three, never as
 * a step, a gate, or a prerequisite. The screen is completable without it.
 */
export default async function WelcomePage() {
  if (!webSignupEnabled()) redirect("/login");

  const session = await getSession();
  if (!session) redirect("/login");

  // Someone who already has a tenant has been here before — the dashboard is
  // the right home for them, and a returning user should never be greeted.
  if (hasTenant(session)) redirect("/dashboard");

  const name = session.firstName?.trim();

  return (
    <div className="flex min-h-svh flex-col items-center justify-center bg-background px-4 py-16">
      <div className="w-full max-w-lg space-y-8">
        <div className="space-y-3">
          <h1 className="text-3xl font-bold tracking-tight">
            {name ? `Welcome, ${name}.` : "Welcome."}
          </h1>
          <p className="text-muted-foreground">
            Storydump posts your Instagram Stories on a schedule, from a media
            library you control.
          </p>
        </div>

        <div className="rounded-lg border bg-card shadow-sm divide-y">
          <ConnectRow
            icon={<Instagram className="h-5 w-5" />}
            title="Instagram"
            description="The account your Stories post to."
            required
          />
          <ConnectRow
            icon={<FolderOpen className="h-5 w-5" />}
            title="Google Drive"
            description="Where your media lives."
            required
          />
          <ConnectRow
            icon={<Send className="h-5 w-5" />}
            title="Telegram"
            description="Get approval requests in a chat."
          />
        </div>

        <p className="text-xs text-muted-foreground">
          You can change any of this later in settings.
        </p>

        <Link
          href="/api/auth/logout"
          className="inline-block text-sm text-muted-foreground transition-colors hover:text-foreground"
        >
          Sign out
        </Link>
      </div>
    </div>
  );
}

/**
 * One connectable service.
 *
 * TENANT_DOOR_REQUIRED — connecting any of these is the first tenant-scoped
 * action, which is where the tenant gets minted. The provisioning door
 * (`ensure_personal_tenant(user_id)`) does not exist yet, and this must not
 * call `get_or_create`: that function is keyed on telegram_chat_id, so for a
 * web tenant it is create-always and would mint a row per request.
 *
 * So the action is rendered as unavailable rather than wired to something that
 * would look like it worked. See src/lib/web-signup.ts, SEAM 2.
 */
function ConnectRow({
  icon,
  title,
  description,
  required = false,
}: {
  icon: React.ReactNode;
  title: string;
  description: string;
  required?: boolean;
}) {
  return (
    <div className="flex items-center gap-4 p-4">
      <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-primary/10 text-primary">
        {icon}
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="font-medium">{title}</span>
          {!required && (
            <span className="rounded bg-muted px-1.5 py-0.5 text-xs text-muted-foreground">
              Optional
            </span>
          )}
        </div>
        <p className="text-sm text-muted-foreground">{description}</p>
      </div>
      <button
        type="button"
        disabled
        title="Not available yet"
        className="inline-flex shrink-0 items-center gap-1.5 rounded-md border px-3 py-1.5 text-sm font-medium text-muted-foreground opacity-50"
      >
        Connect
        <ArrowRight className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}
