/**
 * The owner area's pages (`/ops`, #1723): the rail's one list. A later
 * operator tool is one entry here and one page under `app/(ops)/ops/`, and
 * `/ops` itself redirects to the first.
 */
export type OpsPage = { href: string; label: string };

export const OPS_PAGES: readonly [OpsPage, ...OpsPage[]] = [
  { href: "/ops/waitlist", label: "Waitlist" },
];
