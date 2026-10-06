import { AnalyticsCards } from "@/components/dashboard/analytics-cards";
import { ConditionsPanel } from "@/components/dashboard/conditions-panel";
import { PostingChart } from "@/components/dashboard/posting-chart";
import { PostingMixCard } from "@/components/dashboard/posting-mix-card";
import { RecentActivity } from "@/components/dashboard/recent-activity";
import { DemoVisit } from "@/components/demo/demo-visit";
import { deriveConditions } from "@/lib/conditions";
import { deriveFolderMix, deriveSummary } from "@/lib/dashboard-payloads";
import { sampleWorkspace } from "@/lib/demo/fixtures";

/** The overview's history strip, as on the dashboard: a glance, not a log. */
const HISTORY_LIMIT = 10;

/**
 * The dashboard's Overview for the sample workspace: the same derivations
 * and cards as `(dashboard)/dashboard/page.tsx`, over fixtures. It does not
 * follow the visitor's taps: its figures are a month's counts, which one tap
 * would not move in a real workspace either.
 */
export default function DemoOverviewPage() {
  const workspace = sampleWorkspace(new Date());
  const { stats } = workspace;

  return (
    <div className="space-y-6">
      <DemoVisit page="overview" />

      <div>
        <h1 className="text-2xl font-bold tracking-tight">Overview</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Last 30 days of posting activity.
        </p>
      </div>

      <ConditionsPanel
        conditions={deriveConditions({
          accounts: workspace.accounts.accounts,
          sources: workspace.sources.sources,
          intentsByState: stats.intents_by_state,
        })}
      />

      <AnalyticsCards summary={deriveSummary(stats)} />

      <div className="grid gap-6 lg:grid-cols-2">
        <PostingChart data={stats.posts_by_day} />
        <PostingMixCard mix={deriveFolderMix(stats, workspace.mix)} />
      </div>

      <RecentActivity
        items={workspace.history.slice(0, HISTORY_LIMIT)}
        tz={workspace.config.tz ?? "UTC"}
      />
    </div>
  );
}
