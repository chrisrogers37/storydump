import { redirect } from "next/navigation";
import { getSession } from "@/lib/session";
import { hasTenant } from "@/lib/web-signup";
import { backendFetchJson } from "@/lib/backend";
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

  // Hide the Setup Wizard sidebar entry once onboarding is complete —
  // otherwise clicking it server-side redirects to /dashboard (see
  // dashboard/setup/page.tsx), which looks like a broken link (#464).
  // Without a tenant there is nothing to ask the backend about, and the call
  // below would pass a null chat id through a non-null assertion.
  const initData = hasTenant(session)
    ? await backendFetchJson("init", session.activeChatId!, session.userId, {
        revalidate: 60,
      })
    : null;
  const showSetupWizard = hasTenant(session)
    ? !initData?.setup_state?.onboarding_completed
    : false;

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
