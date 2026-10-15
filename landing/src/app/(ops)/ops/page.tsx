import { redirect } from "next/navigation";
import { OPS_PAGES } from "@/lib/ops-pages";
import { requireOpsPage } from "@/lib/page-guards";

/**
 * `/ops` is the rail's first page. It asks the layout's gate itself before
 * it redirects: Next renders this page beside the layout, so a redirect run
 * for anyone would carry its target into a stranger's 404 page.
 */
export default async function OpsHome() {
  await requireOpsPage();
  redirect(OPS_PAGES[0].href);
}
