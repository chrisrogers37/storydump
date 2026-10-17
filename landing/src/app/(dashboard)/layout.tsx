import { redirect } from "next/navigation";
import { getSession } from "@/lib/session";
import { Sidebar } from "@/components/dashboard/sidebar";
import { DashboardHeader } from "@/components/dashboard/header";
import { noindexMetadata } from "@/lib/seo";

export const metadata = {
  title: "Dashboard",
  ...noindexMetadata,
};

export default async function DashboardLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const session = await getSession();
  if (!session) redirect("/login");

  // The workspace gate lives in middleware, not here, and middleware is its
  // only correct home: Next.js renders layout and page segments in PARALLEL,
  // so a layout that returns early hides the output while the page underneath
  // still runs its fetches. A guard here would read as effective and would
  // not be.
  //
  // The schema has no chat id on a workspace at all, so a tenant-scoped page
  // has no null chat id to pass and no `IS NULL` to land on.

  // No fetch here: a layout runs on every route beneath it, so a request here
  // would be one round trip per dashboard navigation.

  // `relative` is load-bearing: without it, an absolutely positioned
  // descendant (the native <select> Radix renders for a Select in a <form>,
  // `sr-only` text) has no positioned ancestor, sits outside <main>'s clip,
  // and the window scrolls past the frame
  // (main-containing-block-contract.test.ts).

  return (
    <div className="surface-app flex h-screen">
      <Sidebar />
      <div className="flex flex-1 flex-col overflow-hidden">
        <DashboardHeader user={session} />
        <main className="relative flex-1 overflow-y-auto p-6">{children}</main>
      </div>
    </div>
  );
}
