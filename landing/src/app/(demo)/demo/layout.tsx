import type { Metadata } from "next";
import { connection } from "next/server";
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
 * NOT INDEXED, and with no canonical: the sample is a copy of no other page.
 */
export const metadata: Metadata = {
  title: "Sample workspace",
  description:
    "Mark sample Stories posted, skip them or reject them in a made-up workspace. Nothing here is real, nothing is saved, nothing posts.",
  ...noindexMetadata,
  alternates: { canonical: null },
};

/**
 * RENDERED FOR EACH REQUEST, from the request's own time (#1649). The sample's
 * slots are counted from now. An hourly prerender showed the visitor whose
 * request set off its rebuild a copy hours old, its scheduled posts already
 * past.
 */
export default async function DemoLayout({ children }: { children: React.ReactNode }) {
  await connection();
  const sample = sampleWorkspace(new Date());

  return (
    <DemoProvider sample={sample}>
      <DemoShell>{children}</DemoShell>
    </DemoProvider>
  );
}
