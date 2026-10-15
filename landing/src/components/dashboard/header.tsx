"use client";

import Link from "next/link";
import type { SessionUser } from "@/lib/session";
import { NavDrawer } from "@/components/dashboard/nav-drawer";
import { SignOutButton } from "@/components/auth/sign-out-button";

/**
 * The dashboard header.
 *
 * ── The switcher is a link to /workspaces ───────────────────────────────────
 *
 * The thing being switched between is a workspace, which may have no Telegram
 * group at all, and /workspaces is the list of them — it shows role, it holds
 * the create form, and it is reachable without a dashboard to open it from. A
 * second, inline list here would be two implementations of one surface, which
 * is exactly the fork that drifts.
 *
 * So the header LINKS to where you change workspace. It does not name the
 * one you are in: `(dashboard)/layout.tsx` passes no workspace name, so the
 * link reads "Switch workspace" (#1344).
 *
 * NAMING IT IS A CHANGE, NOT A FIX. The session carries the name
 * (`session.workspaces?.find(w => w.id === session.activeWorkspaceId)?.name`),
 * so passing it is two lines — and it would put a workspace name in the
 * chrome of every dashboard page, which is a design question of its own.
 *
 * ── The Owner link (#1723) ───────────────────────────────────────────────
 *
 * Shown to the deployment's operators only (`isOps`, the API's `/me`), and it
 * is the one way into `/ops` from the site. It is a link and not a gate: the
 * owner routes refuse anyone else themselves (`require_ops`).
 */
export function DashboardHeader({ user }: { user: SessionUser }) {
  return (
    <header className="flex h-14 shrink-0 items-center justify-between gap-4 border-b bg-card px-4 lg:px-6">
      <div className="flex min-w-0 items-center gap-3">
        <NavDrawer />

        <Link
          href="/workspaces"
          className="truncate text-sm font-medium text-muted-foreground transition-colors hover:text-foreground"
        >
          Switch workspace
        </Link>

        {user.isOps && (
          <Link
            href="/ops"
            className="shrink-0 text-sm font-medium text-muted-foreground transition-colors hover:text-foreground"
          >
            Owner
          </Link>
        )}
      </div>

      <div className="flex items-center gap-4">
        <span className="truncate text-sm text-muted-foreground">
          {user.displayName || user.email}
        </span>
        <SignOutButton className="shrink-0 text-xs text-muted-foreground transition-colors hover:text-foreground" />
      </div>
    </header>
  );
}
