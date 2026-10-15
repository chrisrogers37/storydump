import { requireWorkspacePage } from "@/lib/page-guards";
import { workspaceFetch } from "@/lib/workspaces";
import {
  HISTORY_STATES,
  deriveFolderMix,
  deriveSourceShares,
  deriveSummary,
  type AccountsResponse,
  type SourcesResponse,
  type StatsResponse,
  type WorkspaceConfig,
} from "@/lib/dashboard-payloads";
import type { CategoryMixResponse } from "@/lib/category-mix";
import { deriveConditions } from "@/lib/conditions";
import type { IntentsResponse } from "@/lib/intents";
import { overviewSetup, parseReadiness, runwayNotes } from "@/lib/readiness";
import { deriveRunway, type RunwayResponse } from "@/lib/runway";
import { parseSourceMix } from "@/lib/source-mix";
import { RouterUnavailable } from "@/components/workspace/router-unavailable";
import { ConditionsPanel } from "@/components/dashboard/conditions-panel";
import { AnalyticsCards } from "@/components/dashboard/analytics-cards";
import { PostingChart } from "@/components/dashboard/posting-chart";
import { PostingMixCard, SourcePostingMixCard } from "@/components/dashboard/posting-mix-card";
import { RecentActivity } from "@/components/dashboard/recent-activity";
import { RunwayCard } from "@/components/dashboard/runway-card";
import { PageHeader } from "@/design/page-header";

/**
 * The overview's history strip: the ten newest outcomes. The API sorts by
 * slot, soonest first, unless asked, and `RecentActivity` draws rows in the
 * order they come, so the read asks for newest first. Ten is a glance, not a
 * log — the full list is the Queue's history and the counts come from
 * `stats`, never from this bounded read.
 */
const HISTORY_LIMIT = 10;

export default async function DashboardPage() {
  const { session, workspaceId } = await requireWorkspacePage();

  // ONE CALL FOR THE COUNTS (#1044).
  //
  // The analytics, the categories and the media count are all one `stats`
  // call — counted where the rows are rather than re-summed from a bounded
  // list, which would be wrong on any workspace past the page size. History is
  // the intent ledger filtered to its terminal states, which is one call rather
  // than a separate endpoint.
  const [
    statsResult,
    runwayResult,
    historyResult,
    accountsResult,
    sourcesResult,
    mixResult,
    configResult,
  ] =
    await Promise.all([
      workspaceFetch<StatsResponse>("stats", workspaceId),
      // Days of content left per account (#1478), counted on the server by
      // the planner's own rule.
      workspaceFetch<RunwayResponse>("runway", workspaceId),
      workspaceFetch<IntentsResponse>(
        `intents?state=${HISTORY_STATES}&order=desc&limit=${HISTORY_LIMIT}`,
        workspaceId,
      ),
      // The condition panel's two lists, read whole — a workspace holds a
      // handful of each, so no destination or folder is cut off by a page
      // size. Its review count comes from `stats`.
      workspaceFetch<AccountsResponse>("accounts", workspaceId),
      workspaceFetch<SourcesResponse>("sources", workspaceId),
      // The mix card's plan per connected folder; what each posted is `stats`.
      // A converted workspace's answer (protocol 2) also carries each
      // account's readiness, which guides setup instead of row existence.
      workspaceFetch<CategoryMixResponse & { protocol_version?: unknown }>(
        "category-mix",
        workspaceId,
      ),
      // The workspace's zone, which Recent Activity's times are read in.
      workspaceFetch<WorkspaceConfig>("", workspaceId),
    ]);

  // EVERY dependency, not just the one that fills the most pixels. Two
  // silently-empty panels state a fact about the account that we did not
  // establish, exactly as three would — and for the condition panel an
  // unread list would state the worst such fact: that nothing needs attention.
  if (
    !statsResult.ok ||
    !runwayResult.ok ||
    !historyResult.ok ||
    !accountsResult.ok ||
    !sourcesResult.ok ||
    !mixResult.ok ||
    !configResult.ok
  ) {
    return <RouterUnavailable what="Your dashboard" />;
  }

  const stats = statsResult.data;
  const summary = deriveSummary(stats);
  // The read says which protocol the mix speaks; nothing here guesses it from
  // the rows. Protocol 1 (or an API that predates the field) has
  // `PostingMixCard`; protocol 2 has `SourcePostingMixCard`, which says when
  // it cannot read the answer rather than drawing a protocol-2 mix by
  // protocol-1 rules.
  const mixProtocol = mixResult.data.protocol_version;
  const legacyMix = mixProtocol === undefined || mixProtocol === 1;
  const sourceMix = legacyMix ? null : parseSourceMix(mixResult.data);
  const conditions = deriveConditions({
    accounts: accountsResult.data.accounts,
    sources: sourcesResult.data.sources,
    intentsByState: stats.intents_by_state,
  });
  // A converted workspace's read carries each account's readiness (null for
  // protocol 1). It says why an account is not ready beside its runway; every
  // other row, and every protocol-1 row, is exactly `deriveRunway`'s.
  const readinessRead = parseReadiness(mixResult.data);
  const notes = runwayNotes(readinessRead);
  const runway = deriveRunway(runwayResult.data).map((row) =>
    notes[row.key] ? { ...row, note: notes[row.key] } : row,
  );
  // A member is told an admin connects things; an unknown role (the list
  // was unreachable) gets the button, which the API refuses if it must.
  const role = session.workspaces?.find((w) => w.id === workspaceId)?.role;
  // Row existence for protocol 1; the server's readiness for protocol 2.
  const { setupStep, readiness } = overviewSetup(
    readinessRead,
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

      <ConditionsPanel conditions={conditions} setupStep={setupStep} readiness={readiness} />

      <AnalyticsCards summary={summary} />

      <RunwayCard rows={runway} belowDays={runwayResult.data.below_days} />

      <div className="grid gap-6 lg:grid-cols-2">
        <PostingChart data={stats.posts_by_day ?? []} />
        {legacyMix ? (
          <PostingMixCard mix={deriveFolderMix(stats, mixResult.data)} />
        ) : (
          <SourcePostingMixCard mix={sourceMix ? deriveSourceShares(stats, sourceMix) : null} />
        )}
      </div>

      <RecentActivity
        items={historyResult.data.intents ?? []}
        tz={configResult.data.tz ?? "UTC"}
      />
    </div>
  );
}
