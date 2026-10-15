import { redirect } from "next/navigation";
import { resolveEntrySession } from "@/lib/entry-session";
import { listMyInvitations, listWorkspaces } from "@/lib/workspaces";
import { CreateWorkspaceForm } from "@/components/workspace/create-workspace-form";
import { InvitationsForYou } from "@/components/workspace/invitations-for-you";
import { SignOutButton } from "@/components/auth/sign-out-button";
import { RouterUnavailable } from "@/components/workspace/router-unavailable";
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
 * ── Why this asks ──────────────────────────────────────────────────────────
 *
 * A workspace has a NAME and no chat id, so there is exactly one thing only
 * the person can supply, and tenant minting is an explicit act at a
 * provisioning door, never a side effect. Asking is the door.
 *
 * ── It is also the single decision on the screen ───────────────────────────
 *
 * Instagram and Drive belong to a workspace, so they cannot be offered before
 * one exists, and a disabled connect row is a dead control that teaches a new
 * user their first act here is to be refused. They live in the workspace, on
 * /dashboard/settings — the Accounts and Integrations tabs.
 */
export default async function WelcomePage() {
  const entry = await resolveEntrySession();
  if (entry.kind === "signed_out") redirect("/login");
  if (entry.kind === "unavailable") {
    // Not "signed out": the cookie is fine, the API could not be asked (a
    // deploy in progress, most often). Sending someone to /login here is a
    // bounce that looks like a broken sign-in.
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

  // Signed in on the way to an invitation: the middleware has already sent
  // them on to its link, spending the cookie that carried it.

  const workspaces = await listWorkspaces();

  // Already has one — they have been here before, and a returning user should
  // never be greeted. /workspaces picks when there is more than one.
  if (workspaces.ok && workspaces.data.length > 0) {
    redirect("/workspaces");
  }

  // Invited, but signed in from the front door rather than the link: say an
  // invitation is waiting before offering a workspace of their own (115).
  const invitations = await listMyInvitations();

  const name = session.displayName?.trim();

  return (
    <Screen width="md" className="space-y-8">
      <PageHeader
        size="lg"
        title={name ? `Welcome, ${name}.` : "Welcome."}
        description="Storydump posts your Instagram Stories on a schedule, from a media library you control. Start by naming a workspace — one brand, one account, one schedule."
      />

      <InvitationsForYou mine={invitations} email={session.email ?? null} />

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
