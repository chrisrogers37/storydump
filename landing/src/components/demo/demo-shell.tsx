"use client";

import type { ReactNode } from "react";
import { Sidebar } from "@/components/dashboard/sidebar";
import { DemoEndPanel } from "@/components/demo/demo-end-panel";
import { DemoHeader } from "@/components/demo/demo-header";
import { DEMO_HOME, DEMO_NAV } from "@/components/demo/nav";

/**
 * The dashboard's visual shell, without a session: the same sidebar and
 * column as `(dashboard)/layout.tsx`, with the sample's header and end panel.
 * A client component because the sidebar's items carry icon components,
 * which cannot cross from a server component.
 */
export function DemoShell({ children }: { children: ReactNode }) {
  return (
    <div className="flex h-screen bg-background">
      <Sidebar items={DEMO_NAV} home={DEMO_HOME} />
      <div className="flex flex-1 flex-col overflow-hidden">
        <DemoHeader />
        <main className="flex-1 overflow-y-auto p-6">
          {children}
          <DemoEndPanel />
        </main>
      </div>
    </div>
  );
}
