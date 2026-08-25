import { redirect } from "next/navigation";
import { getSession } from "@/lib/session";
import { workspaceFetch } from "@/lib/workspaces";
import type {
  AnalyticsResponse,
  CategoriesResponse,
  HistoryResponse,
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

  const [analyticsResult, categoriesResult, historyResult] = await Promise.all([
    workspaceFetch<AnalyticsResponse>("analytics", workspaceId),
    workspaceFetch<CategoriesResponse>("analytics/categories?days=30", workspaceId),
    workspaceFetch<HistoryResponse>("history-detail?limit=10", workspaceId),
  ]);

  // The overview is analytics. Without them there is no partial version of this
  // page worth rendering — a header over three empty panels states a fact about
  // the account that we did not establish.
  if (!analyticsResult.ok) return <RouterUnavailable what="Your dashboard" />;

  const analytics = analyticsResult.data;
  const categories = categoriesResult.ok ? categoriesResult.data : null;
  const history = historyResult.ok ? historyResult.data : null;

  const summary = analytics?.summary ?? {
    total_posts: 0,
    posted: 0,
    skipped: 0,
    rejected: 0,
    failed: 0,
    success_rate: 0,
    avg_per_day: 0,
  };

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Overview</h1>
        <p className="text-sm text-muted-foreground mt-1">
          Last 30 days of posting activity.
        </p>
      </div>

      <AnalyticsCards summary={summary} />

      <div className="grid gap-6 lg:grid-cols-2">
        <PostingChart data={analytics?.daily_counts ?? []} />
        <CategoryBreakdown categories={categories?.categories ?? []} />
      </div>

      <RecentActivity items={history?.items ?? []} />
    </div>
  );
}
