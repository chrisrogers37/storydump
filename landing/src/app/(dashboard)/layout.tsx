import { redirect } from "next/navigation";
import { getSession } from "@/lib/session";
import { hasTenant } from "@/lib/web-signup";
import { backendFetchJson } from "@/lib/backend";
import { Sidebar } from "@/components/dashboard/sidebar";
import { DashboardHeader } from "@/components/dashboard/header";
import { NoTenantEmptyState } from "@/components/dashboard/no-tenant-empty-state";

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

  // Hide the Setup Wizard sidebar entry once onboarding is complete —
  // otherwise clicking it server-side redirects to /dashboard (see
  // dashboard/setup/page.tsx), which looks like a broken link (#464).
  // A tenant-less session gets the empty state for EVERY route under this
  // layout, and its children are never rendered.
  //
  // The gate lives here rather than on each page because this layout is the
  // common ancestor of all nine dashboard routes — so a route added tomorrow
  // inherits it without anyone remembering. A per-page check is an enumeration,
  // and the tenth page is the one that gets missed.
  //
  // It is load-bearing, not cosmetic. Those pages call the backend with
  // session.activeChatId, which is null here; `get_by_chat_id(None)` compiles
  // to `IS NULL`, and `.first()` on that has no ORDER BY. Today no NULL-chat
  // row can exist (chat_settings.telegram_chat_id is NOT NULL, migration 006),
  // so the lookup returns None and require_by_chat_id refuses typed. The moment
  // that column is relaxed the same call returns an arbitrary tenant-less row
  // instead — and require_by_chat_id tests whether the RESULT is None, so it
  // would not raise. Blocking the render is what keeps that from being reachable
  // from this UI at all.
  if (!hasTenant(session)) {
    return (
      <div className="flex h-screen bg-background">
        <Sidebar showSetupWizard={false} />
        <div className="flex flex-1 flex-col overflow-hidden">
          <DashboardHeader user={session} showSetupWizard={false} />
          <main className="flex-1 overflow-y-auto p-6">
            <NoTenantEmptyState />
          </main>
        </div>
      </div>
    );
  }

  const initData = await backendFetchJson(
    "init",
    session.activeChatId!,
    session.userId,
    { revalidate: 60 }
  );
  const showSetupWizard = !initData?.setup_state?.onboarding_completed;

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
