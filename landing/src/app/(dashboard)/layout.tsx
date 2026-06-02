import { redirect } from "next/navigation";
import { getSession } from "@/lib/session";
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
