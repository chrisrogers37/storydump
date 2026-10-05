import { requireWorkspacePage } from "@/lib/page-guards";
import { workspaceFetch } from "@/lib/workspaces";
import {
  HISTORY_STATES,
  deriveFolderMix,
  deriveSummary,
  type AccountsResponse,
  type SourcesResponse,
  type StatsResponse,
} from "@/lib/dashboard-payloads";
import type { CategoryMixResponse } from "@/lib/category-mix";
import { deriveConditions, nextSetupStep } from "@/lib/conditions";
import type { IntentsResponse } from "@/lib/intents";
import { RouterUnavailable } from "@/components/workspace/router-unavailable";
import { ConditionsPanel } from "@/components/dashboard/conditions-panel";
import { AnalyticsCards } from "@/components/dashboard/analytics-cards";
import { PostingChart } from "@/components/dashboard/posting-chart";
import { PostingMixCard } from "@/components/dashboard/posting-mix-card";
import { RecentActivity } from "@/components/dashboard/recent-activity";
import { PageHeader } from "@/design/page-header";

/**
 * The overview's history strip. Ten is a glance, not a log — the full list
 * is the Queue's history and the counts come from `stats`, never from this
 * bounded read (`01` H5).
 */
const HISTORY_LIMIT = 10;

export default async function DashboardPage() {
  const { session, workspaceId } = await requireWorkspacePage();

  // THREE CALLS BECAME TWO (#1044).
  //
  // `analytics`, `analytics/categories` and `init`'s media count are all one
  // `stats` call now — counted where the rows are rather than re-summed from a
  // bounded list, which is what made the old figures wrong on any workspace
  // past the page size. History is the intent ledger filtered to its terminal
  // states, which is one call rather than a separate endpoint.
  const [statsResult, historyResult, accountsResult, sourcesResult, mixResult] =
    await Promise.all([
      workspaceFetch<StatsResponse>("stats", workspaceId),
      workspaceFetch<IntentsResponse>(
        `intents?state=${HISTORY_STATES}&limit=${HISTORY_LIMIT}`,
        workspaceId,
      ),
      // The condition panel's two lists, read whole — a workspace holds a
      // handful of each, so no destination or folder is cut off by a page
      // size. Its review count comes from `stats`.
      workspaceFetch<AccountsResponse>("accounts", workspaceId),
      workspaceFetch<SourcesResponse>("sources", workspaceId),
      // The mix card's plan per connected folder; what each posted is `stats`.
      workspaceFetch<CategoryMixResponse>("category-mix", workspaceId),
    ]);

  // EVERY dependency, not just the one that fills the most pixels. Two
  // silently-empty panels state a fact about the account that we did not
  // establish, exactly as three would — and for the condition panel an
  // unread list would state the worst such fact: that nothing needs attention.
  if (
    !statsResult.ok ||
    !historyResult.ok ||
    !accountsResult.ok ||
    !sourcesResult.ok ||
    !mixResult.ok
  ) {
    return <RouterUnavailable what="Your dashboard" />;
  }

  const stats = statsResult.data;
  const summary = deriveSummary(stats);
  const mix = deriveFolderMix(stats, mixResult.data);
  const conditions = deriveConditions({
    accounts: accountsResult.data.accounts,
    sources: sourcesResult.data.sources,
    intentsByState: stats.intents_by_state,
  });
  // A member is told an admin connects things; an unknown role (the list
  // was unreachable) gets the button, which the API refuses if it must.
  const role = session.workspaces?.find((w) => w.id === workspaceId)?.role;
  const setupStep = nextSetupStep(
    {
      accounts: accountsResult.data.accounts,
      sources: sourcesResult.data.sources,
    },
    { isAdmin: role !== "member" },
  );

  return (
    <div className="space-y-6">
      <PageHeader
        title="Overview"
        description="Last 30 days of posting activity."
      />

      <ConditionsPanel conditions={conditions} setupStep={setupStep} />

      <AnalyticsCards summary={summary} />

      <div className="grid gap-6 lg:grid-cols-2">
        <PostingChart data={stats.posts_by_day ?? []} />
        <PostingMixCard mix={mix} />
      </div>

      <RecentActivity items={historyResult.data.intents ?? []} />
    </div>
  );
}
