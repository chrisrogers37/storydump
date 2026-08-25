import { redirect } from "next/navigation";
import { getSession } from "@/lib/session";
import { workspaceFetch } from "@/lib/workspaces";
import type {
  HistoryResponse,
  QueueResponse,
  ScheduleResponse,
} from "@/lib/dashboard-payloads";
import { RouterUnavailable } from "@/components/workspace/router-unavailable";
import { ContentCalendar } from "@/components/dashboard/media/content-calendar";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

export default async function CalendarPage() {
  const session = await getSession().catch(() => null);
  if (!session) redirect("/login");
  const workspaceId = session.activeWorkspaceId;
  if (!workspaceId) redirect("/welcome");
  const [historyResult, queueResult, scheduleResult] = await Promise.all([
    workspaceFetch<HistoryResponse>("history-detail?limit=15", workspaceId),
    workspaceFetch<QueueResponse>("queue-detail?limit=10", workspaceId),
    workspaceFetch<ScheduleResponse>(
      "analytics/schedule-preview?slots=15",
      workspaceId,
    ),
  ]);

  // The calendar is the three of these side by side. One missing leaves a
  // column of zeros next to real data, which reads as "nothing scheduled"
  // rather than as "we could not ask".
  if (!historyResult.ok || !queueResult.ok || !scheduleResult.ok) {
    return <RouterUnavailable what="Your calendar" />;
  }

  const history = historyResult.data;
  const queue = queueResult.data;
  const schedule = scheduleResult.data;

  const historyItems = history?.items ?? [];
  const queueItems = queue?.items ?? [];
  const scheduleSlots = schedule?.slots ?? [];

  return (
    <div className="space-y-6">
      {/* Summary stats */}
      <div className="grid gap-4 sm:grid-cols-3">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Posts Today
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{queue?.posts_today ?? 0}</div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              In Queue
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">
              {queue?.total_in_flight ?? 0}
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Posting Rate
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">
              {schedule?.posts_per_day ?? 0}/day
            </div>
            <p className="text-xs text-muted-foreground mt-1">
              Every {schedule?.interval_minutes ? Math.round(schedule.interval_minutes) : "—"} min
            </p>
          </CardContent>
        </Card>
      </div>

      <ContentCalendar
        history={historyItems}
        queue={queueItems}
        schedule={scheduleSlots}
      />
    </div>
  );
}
