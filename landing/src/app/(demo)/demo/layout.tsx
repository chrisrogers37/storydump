import type { Metadata } from "next";
import { DemoProvider } from "@/components/demo/demo-provider";
import { DemoShell } from "@/components/demo/demo-shell";
import { sampleWorkspace } from "@/lib/demo/fixtures";
import { noindexMetadata } from "@/lib/seo";

/**
 * The sample workspace (#1480): the dashboard's Overview, Queue and Calendar
 * for a made-up workspace, open to a visitor who has not signed in. Nothing
 * here reaches the API: the stories are fixtures and every decision is made
 * in the browser (`demo-isolation-contract.test.ts`).
 *
 * NOT INDEXED, and it names no canonical of its own: the root layout's
 * canonical points at the home page, which a sample is not a copy of.
 */
export const metadata: Metadata = {
  title: "Sample workspace",
  description:
    "Approve, skip and reject sample Stories in a made-up workspace. Nothing here is real, nothing is saved, nothing posts.",
  ...noindexMetadata,
  alternates: { canonical: null },
};

/** Prerendered and rebuilt hourly, so the sample's times stay current. */
export const revalidate = 3600;

export default function DemoLayout({ children }: { children: React.ReactNode }) {
  const workspace = sampleWorkspace(new Date());

  return (
    <DemoProvider
      queue={workspace.queue}
      history={workspace.history}
      tz={workspace.config.tz ?? "UTC"}
    >
      <DemoShell>{children}</DemoShell>
    </DemoProvider>
  );
}
