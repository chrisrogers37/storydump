import { redirect } from "next/navigation";
import { getSession } from "@/lib/session";
import { workspaceFetch } from "@/lib/workspaces";
import type { InitResponse } from "@/lib/dashboard-payloads";
import { Sidebar } from "@/components/dashboard/sidebar";
import { DashboardHeader } from "@/components/dashboard/header";

export const metadata = {
  title: "Dashboard — Storydump",
};

export default async function DashboardLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const session = await getSession();
  if (!session) redirect("/login");

  // The workspace gate lives in middleware, not here. This layout used to
  // carry it, and the reasoning it carried is worth keeping because it is what
  // makes middleware the only correct home: Next.js renders layout and page
  // segments in PARALLEL, so a layout that returns early hides the output while
  // the page underneath still runs its fetches. The guard read as effective and
  // was not. Measured on a warm route — one request, one backend call, with
  // this guard in place.
  //
  // What that gate protected against is also gone at the source. It existed
  // because the tenant-scoped pages called the backend with a null chat id,
  // which compiled to `IS NULL` against a table where no null-chat row could
  // exist — until someone relaxed the column, at which point the same call
  // would have returned an arbitrary stranger's row. The target schema has no
  // chat id on a workspace at all, so there is no null to pass and no `IS NULL`
  // to land on. The class is retired rather than re-guarded.
  const workspaceId = session.activeWorkspaceId;

  // Sidebar entry is hidden once onboarding is complete — otherwise it points
  // at a route that redirects, which reads as a broken link (#464). Unknown
  // means hidden: showing a wizard we cannot confirm is needed is worse than
  // omitting a link the user can still reach from settings.
  const initResult = workspaceId
    ? await workspaceFetch<InitResponse>("init", workspaceId)
    : null;
  const showSetupWizard = Boolean(
    initResult?.ok && !initResult.data.setup_state?.onboarding_completed,
  );

  return (
    <div className="flex h-screen bg-background">
      <Sidebar showSetupWizard={showSetupWizard} />
      <div className="flex flex-1 flex-col overflow-hidden">
        <DashboardHeader user={session} showSetupWizard={showSetupWizard} />
        <main className="flex-1 overflow-y-auto p-6">{children}</main>
      </div>
    </div>
  );
}
