import { redirect } from "next/navigation";
import { cookies } from "next/headers";
import { resolveEntrySession } from "@/lib/entry-session";
import { listWorkspaces } from "@/lib/workspaces";
import { CreateWorkspaceForm } from "@/components/workspace/create-workspace-form";
import { SignOutButton } from "@/components/auth/sign-out-button";
import { RouterUnavailable } from "@/components/workspace/router-unavailable";
import { INVITE_COOKIE } from "@/lib/session";
import { noindexMetadata } from "@/lib/seo";
import { Card } from "@/components/ui/card";
import { PageHeader } from "@/design/page-header";
import { Screen } from "@/design/screen";

export const metadata = {
  title: "Welcome",
  ...noindexMetadata,
};

/**
 * First run: name a workspace.
 *
 * ── Why this asks, when the previous version deliberately did not ──────────
 *
 * The version this replaces said: *"It deliberately does NOT ask the user to
 * create a workspace. Tenant minting is lazy — the tenant is minted at the
 * first tenant-scoped action."* That was correct for the legacy schema, where a
 * tenant was a `chat_settings` row keyed on a Telegram chat id — there was
 * nothing a person could have been asked for, so it had to happen implicitly.
 *
 * On the target schema a workspace has a NAME and no chat id, so there is
 * exactly one thing only the person can supply, and the design plan's own rule
 * applies: tenant minting is an explicit act at a provisioning door, never a
 * side effect. Asking is the door.
 *
 * ── It is also the single decision on the screen ───────────────────────────
 *
 * The previous version offered three connect rows, all disabled. Instagram and
 * Drive belong to a workspace, so they cannot be offered before one exists —
 * that is what made them disabled, and three dead controls is a screen that
 * teaches a new user their first act here is to be refused. They live in the
 * workspace now, on /dashboard/settings — the Accounts and Integrations
 * tabs.
 */
export default async function WelcomePage() {
  const entry = await resolveEntrySession();
  if (entry.kind === "signed_out") redirect("/login");
  if (entry.kind === "unavailable") {
    // Not "signed out": the cookie is fine, the API could not be asked (a
    // deploy in progress, most often). Sending someone to /login here is the
    // bounce that looked like a broken sign-in on 2026-09-04.
    return (
      <Screen width="md">
        <RouterUnavailable
          what="Your account"
          detail="Storydump is restarting or briefly unreachable — nothing was lost. Try again in a moment."
          retryHref="/welcome"
        />
      </Screen>
    );
  }
  const session = entry.session;

  // Signed in on the way to an invitation — finish that instead of greeting
  // them. The cookie is cleared where it is spent, in the accept handler.
  const invite = (await cookies()).get(INVITE_COOKIE)?.value;
  if (invite) redirect(`/join/${encodeURIComponent(invite)}`);

  const workspaces = await listWorkspaces();

  // Already has one — they have been here before, and a returning user should
  // never be greeted. /workspaces picks when there is more than one.
  if (workspaces.ok && workspaces.data.length > 0) {
    redirect("/workspaces");
  }

  const name = session.displayName?.trim();

  return (
    <Screen width="md" className="space-y-8">
      <PageHeader
        size="lg"
        title={name ? `Welcome, ${name}.` : "Welcome."}
        description="Storydump posts your Instagram Stories on a schedule, from a media library you control. Start by naming a workspace — one brand, one account, one schedule."
      />

      {workspaces.ok ? (
        <>
          <Card className="p-6">
            <CreateWorkspaceForm autoFocus />
          </Card>
          <p className="text-xs text-muted-foreground">
            You can rename it later, and add Instagram, Drive and Telegram
            once it exists.
          </p>
        </>
      ) : (
        <RouterUnavailable what="Creating a workspace" />
      )}

      <SignOutButton className="inline-block text-sm text-muted-foreground transition-colors hover:text-foreground" />
    </Screen>
  );
}
