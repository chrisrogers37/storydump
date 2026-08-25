"use client";

import { useRouter } from "next/navigation";
import Link from "next/link";
import { Menu } from "lucide-react";
import type { SessionUser } from "@/lib/session";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetTrigger } from "@/components/ui/sheet";
import { Sidebar } from "@/components/dashboard/sidebar";

/**
 * The dashboard header.
 *
 * ── The inline switcher is gone, and that is a consolidation, not a loss ────
 *
 * This used to hold a dropdown that fetched /api/instances, listed the user's
 * Telegram groups, and switched between them. Two reasons it is now a link:
 *
 *  1. It listed GROUPS. The thing being switched between is now a workspace,
 *     which may have no Telegram group at all, so the list it drew from does
 *     not exist any more.
 *  2. /workspaces is that list, and it is a better one — it shows role, it
 *     holds the create form, and it is reachable without a dashboard to open
 *     it from. Keeping both would be two implementations of one surface, which
 *     is exactly the fork that drifts.
 *
 * So the header states which workspace you are in and links to where you change
 * it. One place to keep correct.
 */
export function DashboardHeader({
  user,
  workspaceName,
  showSetupWizard = true,
}: {
  user: SessionUser;
  workspaceName?: string;
  showSetupWizard?: boolean;
}) {
  const router = useRouter();

  async function handleLogout() {
    await fetch("/api/auth/logout", { method: "POST" });
    router.push("/login");
  }

  return (
    <header className="flex h-14 shrink-0 items-center justify-between gap-4 border-b bg-background px-4 lg:px-6">
      <div className="flex min-w-0 items-center gap-3">
        <Sheet>
          <SheetTrigger asChild>
            <Button
              variant="ghost"
              size="icon"
              className="lg:hidden"
              aria-label="Open navigation"
            >
              <Menu className="h-5 w-5" />
            </Button>
          </SheetTrigger>
          <SheetContent side="left" className="w-64 p-0">
            <Sidebar showSetupWizard={showSetupWizard} />
          </SheetContent>
        </Sheet>

        <Link
          href="/workspaces"
          className="truncate text-sm font-medium text-muted-foreground transition-colors hover:text-foreground"
        >
          {workspaceName || "Switch workspace"}
        </Link>
      </div>

      <div className="flex items-center gap-4">
        <span className="truncate text-sm text-muted-foreground">
          {user.displayName || user.email}
        </span>
        <button
          onClick={handleLogout}
          className="shrink-0 text-xs text-muted-foreground transition-colors hover:text-foreground"
        >
          Sign out
        </button>
      </div>
    </header>
  );
}
