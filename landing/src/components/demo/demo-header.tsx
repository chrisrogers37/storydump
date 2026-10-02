"use client";

import { Menu } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetTitle, SheetTrigger } from "@/components/ui/sheet";
import { Sidebar } from "@/components/dashboard/sidebar";
import { DemoCta } from "@/components/demo/demo-cta";
import { DEMO_HOME, DEMO_NAV } from "@/components/demo/nav";

/**
 * The sample's header: the persistent banner where the dashboard's header has
 * the workspace link and Sign out. No session, so nothing here names a person.
 *
 * The drawer mounts the shared `Sidebar` in its `mobile` variant behind an
 * `lg:hidden` trigger, the same pairing as the dashboard's header (#1363):
 * the drawer is the navigation below the breakpoint, the aside above it.
 */
export function DemoHeader() {
  return (
    <header className="flex min-h-14 shrink-0 flex-wrap items-center justify-between gap-x-4 gap-y-2 border-b bg-background px-4 py-2 lg:px-6">
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
            <SheetTitle className="sr-only">Navigation</SheetTitle>
            <Sidebar mobile items={DEMO_NAV} home={DEMO_HOME} />
          </SheetContent>
        </Sheet>

        <p className="text-sm text-muted-foreground">
          <span className="font-medium text-foreground">Sample workspace.</span>{" "}
          Nothing here is real, nothing is saved, nothing posts.
        </p>
      </div>

      <DemoCta />
    </header>
  );
}
