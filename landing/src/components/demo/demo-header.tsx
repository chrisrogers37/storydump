"use client";

import { NavDrawer } from "@/components/dashboard/nav-drawer";
import { DemoCta } from "@/components/demo/demo-cta";
import { DEMO_HOME, DEMO_NAV } from "@/components/demo/nav";

/**
 * The sample's header: the persistent banner where the dashboard's header has
 * the workspace link and Sign out. No session, so nothing here names a person.
 *
 * The drawer is the dashboard's own (`NavDrawer`) with the sample's entries:
 * the navigation below the breakpoint, the aside above it (#1363).
 */
export function DemoHeader() {
  return (
    <header className="flex min-h-14 shrink-0 flex-wrap items-center justify-between gap-x-4 gap-y-2 border-b bg-background px-4 py-2 lg:px-6">
      <div className="flex min-w-0 items-center gap-3">
        <NavDrawer items={DEMO_NAV} home={DEMO_HOME} />

        <p className="text-sm text-muted-foreground">
          <span className="font-medium text-foreground">Sample workspace.</span>{" "}
          Nothing here is real, nothing is saved, nothing posts.
        </p>
      </div>

      <DemoCta />
    </header>
  );
}
