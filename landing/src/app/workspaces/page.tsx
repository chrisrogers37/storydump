import { redirect } from "next/navigation";
import { Plus } from "lucide-react";
import { getSession } from "@/lib/session";
import { listWorkspaces } from "@/lib/workspaces";
import { WorkspaceList } from "@/components/workspace/workspace-list";
import { CreateWorkspaceForm } from "@/components/workspace/create-workspace-form";
import { RouterUnavailable } from "@/components/workspace/router-unavailable";
import { siteConfig } from "@/config/site";

export const metadata = {
  title: `Workspaces — ${siteConfig.name}`,
};

/**
 * Every workspace this user belongs to.
 *
 * Replaces /instances, which listed Telegram groups the user's chat id appeared
 * in. That page could only ever show groups, so a user without Telegram saw an
 * empty list and no way to change it — the funnel this whole slice exists to
 * remove.
 *
 * ── Create is at the bottom, not the top ───────────────────────────────────
 *
 * A user reaching this page with workspaces is here to switch, which is the
 * common case by a wide margin; creating another is rare. Putting the form
 * above the list would push the thing they came for below the fold on a phone
 * and make the rare act the first thing they read.
 *
 * A user with NO workspaces never sees this page at all — /welcome owns that
 * state and is a better version of it, so there is no empty state to design.
 */
export default async function WorkspacesPage() {
  const session = await getSession().catch(() => null);
  if (!session) redirect("/login");

  const workspaces = await listWorkspaces();

  if (!workspaces.ok) {
    return (
      <Shell>
        <RouterUnavailable what="Your workspaces" />
      </Shell>
    );
  }

  if (workspaces.data.length === 0) redirect("/welcome");

  return (
    <Shell>
      <div className="space-y-2">
        <h1 className="text-2xl font-bold tracking-tight">Workspaces</h1>
        <p className="text-sm text-muted-foreground">
          Each workspace has its own media, schedule and connected accounts.
        </p>
      </div>

      <WorkspaceList
        workspaces={workspaces.data}
        activeId={session.activeWorkspaceId}
      />

      {/* Deliberately NOT a card. Given the same border, radius and background
          as the list above it, this read as a third workspace whose name had
          not loaded — or as an empty text field. Both are worse than plain
          text: the control has to be legible as an action, and the cheapest
          way to make it one is to stop it looking like the data. */}
      <details className="group">
        <summary className="inline-flex cursor-pointer list-none items-center gap-1.5 text-sm font-medium text-muted-foreground transition-colors hover:text-foreground">
          <Plus className="h-4 w-4 transition-transform group-open:rotate-45" aria-hidden />
          New workspace
        </summary>
        <div className="pt-4">
          <CreateWorkspaceForm />
        </div>
      </details>
    </Shell>
  );
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-svh flex-col items-center bg-background px-4 py-16">
      <div className="w-full max-w-md space-y-6">{children}</div>
    </div>
  );
}
