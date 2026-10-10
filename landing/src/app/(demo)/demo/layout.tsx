import type { Metadata } from "next";
import { DemoShell } from "@/components/demo/demo-shell";
import { noindexMetadata } from "@/lib/seo";

/**
 * The sample workspace (#1480): the dashboard's Overview, Queue and Calendar
 * for a made-up workspace, open to a visitor who has not signed in. Nothing
 * here reaches the API: the stories are fixtures and every decision is made
 * in the browser (`demo-isolation-contract.test.ts`).
 *
 * NOT INDEXED, and with no canonical: the sample is a copy of no other page.
 *
 * PRERENDERED, AND IT HOLDS NO SAMPLE (#1649). The sample's slots are counted
 * from now, so a copy made ahead of the visit is wrong by however long it
 * waited: an hourly rebuild served the visitor who set it off scheduled posts
 * already past. The visitor's browser builds the sample when the page opens
 * (`DemoProvider`), and the page stays on the CDN.
 */
export const metadata: Metadata = {
  title: "Sample workspace",
  description:
    "Mark sample Stories posted, skip them or reject them in a made-up workspace. Nothing here is real, nothing is saved, nothing posts.",
  ...noindexMetadata,
  alternates: { canonical: null },
};

export default function DemoLayout({ children }: { children: React.ReactNode }) {
  return <DemoShell>{children}</DemoShell>;
}
