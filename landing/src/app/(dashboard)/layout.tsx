import { redirect } from "next/navigation";
import { getSession } from "@/lib/session";
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

  // No fetch here any more. The only thing this layout ever called the router
  // for was `init`, to decide whether to show a Setup Wizard nav entry — and
  // that entry pointed at a page this change deletes. The link went, the flag
  // that gated it went, and the call that computed the flag went with them.
  //
  // Removing a request from a layout is worth more than it looks: a layout runs
  // on every route beneath it, so this was one round trip per dashboard
  // navigation spent on a boolean nobody can act on.

  return (
    <div className="flex h-screen bg-background">
      <Sidebar />
      <div className="flex flex-1 flex-col overflow-hidden">
        <DashboardHeader user={session} />
        <main className="flex-1 overflow-y-auto p-6">{children}</main>
      </div>
    </div>
  );
}
