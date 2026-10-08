"use client";

import { useMemo } from "react";
import { AnalyticsCards } from "@/components/dashboard/analytics-cards";
import { ConditionsPanel } from "@/components/dashboard/conditions-panel";
import { PostingChart } from "@/components/dashboard/posting-chart";
import { PostingMixCard } from "@/components/dashboard/posting-mix-card";
import { RecentActivity } from "@/components/dashboard/recent-activity";
import { useDemo } from "@/components/demo/demo-provider";
import { deriveConditions } from "@/lib/conditions";
import { deriveFolderMix, deriveSummary } from "@/lib/dashboard-payloads";
import { overviewOf } from "@/lib/demo/overview";

/** The overview's history strip, as on the dashboard: a glance, not a log. */
const HISTORY_LIMIT = 10;

/**
 * The dashboard's Overview for the sample workspace: the same derivations and
 * cards as `(dashboard)/dashboard/page.tsx`, over the sample as the visitor
 * has left it (#1649). A story decided in the Queue moves the counts it would
 * move in a real workspace, and heads Recent activity.
 */
export function DemoOverview() {
  const { sample, state, tz } = useDemo();
  const { stats, activity } = useMemo(
    () => overviewOf(sample, state.queue),
    [sample, state.queue],
  );

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Overview</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Last 30 days of posting activity.
        </p>
      </div>

      <ConditionsPanel
        conditions={deriveConditions({
          accounts: sample.accounts.accounts,
          sources: sample.sources.sources,
          intentsByState: stats.intents_by_state,
        })}
      />

      <AnalyticsCards summary={deriveSummary(stats)} />

      <div className="grid gap-6 lg:grid-cols-2">
        <PostingChart data={stats.posts_by_day} />
        <PostingMixCard mix={deriveFolderMix(stats, sample.mix)} />
      </div>

      <RecentActivity items={activity.slice(0, HISTORY_LIMIT)} tz={tz} />
    </div>
  );
}
