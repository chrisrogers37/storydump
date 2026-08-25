import { redirect } from "next/navigation";
import { getSession } from "@/lib/session";
import { workspaceFetch } from "@/lib/workspaces";
import {
  HISTORY_STATES,
  deriveCategories,
  deriveSummary,
  type IntentsResponse,
  type StatsResponse,
} from "@/lib/dashboard-payloads";
import { RouterUnavailable } from "@/components/workspace/router-unavailable";
import { AnalyticsCards } from "@/components/dashboard/analytics-cards";
import { PostingChart } from "@/components/dashboard/posting-chart";
import { CategoryBreakdown } from "@/components/dashboard/category-breakdown";
import { RecentActivity } from "@/components/dashboard/recent-activity";

export default async function DashboardPage() {
  // Deduped with layout via React cache() — no extra JWT verification
  const session = await getSession().catch(() => null);
  if (!session) redirect("/login");
  // Middleware already required a selected workspace to reach any route under
  // /dashboard. Repeated because a page is reachable in tests and in a direct
  // render without it, and `activeWorkspaceId!` would be a non-null assertion
  // on a value that is legitimately null for every brand-new user.
  const workspaceId = session.activeWorkspaceId;
  if (!workspaceId) redirect("/welcome");

  // THREE CALLS BECAME TWO (#1044).
  //
  // `analytics`, `analytics/categories` and `init`'s media count are all one
  // `stats` call now — counted where the rows are rather than re-summed from a
  // bounded list, which is what made the old figures wrong on any workspace
  // past the page size. History is the intent ledger filtered to its terminal
  // states, which is one call rather than a separate endpoint.
  const [statsResult, historyResult] = await Promise.all([
    workspaceFetch<StatsResponse>("stats", workspaceId),
    workspaceFetch<IntentsResponse>(
      `intents?state=${HISTORY_STATES}&limit=10`,
      workspaceId,
    ),
  ]);

  // EVERY dependency, not just the one that fills the most pixels. Two
  // silently-empty panels state a fact about the account that we did not
  // establish, exactly as three would.
  if (!statsResult.ok || !historyResult.ok) {
    return <RouterUnavailable what="Your dashboard" />;
  }

  const stats = statsResult.data;
  const summary = deriveSummary(stats);
  const categories = deriveCategories(stats);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Overview</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Last 30 days of posting activity.
        </p>
      </div>

      <AnalyticsCards summary={summary} />

      <div className="grid gap-6 lg:grid-cols-2">
        <PostingChart data={stats.posts_by_day ?? []} />
        <CategoryBreakdown categories={categories} />
      </div>

      <RecentActivity items={historyResult.data.intents ?? []} />
    </div>
  );
}
