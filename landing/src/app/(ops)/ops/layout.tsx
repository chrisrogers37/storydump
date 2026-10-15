import Link from "next/link";
import { Wordmark } from "@/design/brand";
import { OPS_PAGES } from "@/lib/ops-pages";
import { requireOpsPage } from "@/lib/page-guards";
import { noindexMetadata } from "@/lib/seo";

export const metadata = {
  title: "Owner",
  ...noindexMetadata,
};

const RAIL_LINK =
  "rounded-full px-3 py-2 text-sm font-medium text-muted-foreground transition-colors hover:bg-muted hover:text-foreground";

/**
 * The owner area (#1723): the deployment's own tools, for the people in
 * the API's `OPS_USER_IDS`. It needs no workspace, so it is a route group of
 * its own rather than a page under `/dashboard`, and its rail is built from
 * `OPS_PAGES`, so a later tool is one entry and one page.
 *
 * ── Anyone else gets the site's ordinary 404 ───────────────────────────────
 *
 * No session, a session the API could not resolve, or a person who is not an
 * operator: `notFound()`, never a redirect to /login (`requireOpsPage`).
 *
 * `isOps` decides this 404 and opens nothing (`SessionUser.isOps`). Next
 * renders a layout and its page in parallel, so each page refuses on its own
 * too: the waitlist on its read's answer, `/ops` through the same gate.
 */
export default async function OpsLayout({ children }: { children: React.ReactNode }) {
  await requireOpsPage();

  return (
    <div className="surface-app flex min-h-svh flex-col lg:flex-row">
      <aside className="shrink-0 border-b bg-card lg:w-56 lg:border-r lg:border-b-0">
        <div className="flex h-14 items-center gap-2 px-4 lg:border-b">
          <Wordmark className="text-lg" />
          <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Owner
          </span>
        </div>
        <nav aria-label="Owner" className="flex flex-wrap gap-1 px-3 pb-3 lg:flex-col lg:p-3">
          {OPS_PAGES.map((page) => (
            <Link key={page.href} href={page.href} className={RAIL_LINK}>
              {page.label}
            </Link>
          ))}
          <Link href="/dashboard" className={`${RAIL_LINK} lg:mt-4`}>
            ← Back to the dashboard
          </Link>
        </nav>
      </aside>
      <main className="min-w-0 flex-1 p-4 lg:p-6">{children}</main>
    </div>
  );
}
