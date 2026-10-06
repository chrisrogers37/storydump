import { redirect } from "next/navigation";
import { Plus } from "lucide-react";
import { resolveEntrySession } from "@/lib/entry-session";
import { listWorkspaces } from "@/lib/workspaces";
import { WorkspaceList } from "@/components/workspace/workspace-list";
import { CreateWorkspaceForm } from "@/components/workspace/create-workspace-form";
import { RouterUnavailable } from "@/components/workspace/router-unavailable";
import { noindexMetadata } from "@/lib/seo";
import { PageHeader } from "@/design/page-header";
import { Screen } from "@/design/screen";

export const metadata = {
  title: "Workspaces",
  ...noindexMetadata,
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
  const entry = await resolveEntrySession();
  if (entry.kind === "signed_out") redirect("/login");
  if (entry.kind === "unavailable") {
    return (
      <Screen width="md" align="top">
        <RouterUnavailable what="Your account" detail="Storydump is restarting or briefly unreachable — nothing was lost. Try again in a moment." retryHref="/workspaces" />
      </Screen>
    );
  }
  const session = entry.session;

  const workspaces = await listWorkspaces();

  if (!workspaces.ok) {
    return (
      <Screen width="md" align="top">
        <RouterUnavailable what="Your workspaces" />
      </Screen>
    );
  }

  if (workspaces.data.length === 0) redirect("/welcome");

  return (
    <Screen width="md" align="top">
      <PageHeader
        title="Workspaces"
        description="Each workspace has its own media, schedule and connected accounts."
      />

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
    </Screen>
  );
}
