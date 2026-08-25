import { redirect } from "next/navigation";
import { getSession } from "@/lib/session";
import { workspaceFetch } from "@/lib/workspaces";
import {
  HISTORY_STATES,
  QUEUE_STATES,
  SCHEDULED_STATES,
  type IntentRow,
  type IntentsResponse,
  type StatsResponse,
  type WorkspaceConfig,
} from "@/lib/dashboard-payloads";
import { RouterUnavailable } from "@/components/workspace/router-unavailable";
import { ContentCalendar } from "@/components/dashboard/media/content-calendar";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

/** The calendar's lanes are all the intent ledger now, filtered by state. */
const laneItem = (i: IntentRow) => ({
  media_name: i.file_name,
  category: i.category ?? "uncategorised",
  status: i.state,
});

/** Today, in the WORKSPACE's timezone — `daily_post_counts.local_date` is local. */
function todayIn(tz: string | null): string {
  try {
    return new Intl.DateTimeFormat("en-CA", {
      timeZone: tz ?? "UTC",
    }).format(new Date());
  } catch {
    return new Intl.DateTimeFormat("en-CA", { timeZone: "UTC" }).format(new Date());
  }
}

export default async function CalendarPage() {
  const session = await getSession().catch(() => null);
  if (!session) redirect("/login");
  const workspaceId = session.activeWorkspaceId;
  if (!workspaceId) redirect("/welcome");

  const [historyResult, queueResult, scheduleResult, statsResult, configResult] =
    await Promise.all([
      workspaceFetch<IntentsResponse>(
        `intents?state=${HISTORY_STATES}&limit=15`,
        workspaceId,
      ),
      workspaceFetch<IntentsResponse>(
        `intents?state=${QUEUE_STATES}&limit=10`,
        workspaceId,
      ),
      workspaceFetch<IntentsResponse>(
        `intents?state=${SCHEDULED_STATES}&limit=15`,
        workspaceId,
      ),
      workspaceFetch<StatsResponse>("stats", workspaceId),
      workspaceFetch<WorkspaceConfig>("", workspaceId),
    ]);

  // The calendar is these side by side. One missing leaves a column of zeros
  // next to real data, which reads as "nothing scheduled" rather than as "we
  // could not ask".
  if (
    !historyResult.ok ||
    !queueResult.ok ||
    !scheduleResult.ok ||
    !statsResult.ok ||
    !configResult.ok
  ) {
    return <RouterUnavailable what="Your calendar" />;
  }

  const stats = statsResult.data;
  const config = configResult.data;

  const historyItems = (historyResult.data.intents ?? []).map((i) => ({
    ...laneItem(i),
    posted_at: i.entered_state_at,
  }));

  // A queued intent with no slot cannot be placed on a calendar. Dropping it
  // here is not hiding it — it has no date to be drawn at.
  const queueItems = (queueResult.data.intents ?? [])
    .filter((i) => i.schedule_slot_at !== null)
    .map((i) => ({ ...laneItem(i), scheduled_for: i.schedule_slot_at as string }));

  const scheduleSlots = (scheduleResult.data.intents ?? [])
    .filter((i) => i.schedule_slot_at !== null)
    .map((i) => ({
      slot_time: i.schedule_slot_at as string,
      predicted_category: i.category,
    }));

  // Counted where the rows are, not re-summed from the bounded lists above.
  const today = todayIn(config.tz);
  const postsToday =
    (stats.posts_by_day ?? []).find((d) => d.local_date.startsWith(today))
      ?.count ?? 0;
  const inFlight = QUEUE_STATES.split(",").reduce(
    (a, s) => a + (stats.intents_by_state?.[s] ?? 0),
    0,
  );

  // Derived from the workspace's own config rather than served: the posting
  // window divided by the daily target. Null config means no answer, not zero.
  const perDay = config.posts_per_day;
  const windowHours =
    config.posting_hours_start !== null && config.posting_hours_end !== null
      ? config.posting_hours_end - config.posting_hours_start
      : null;
  const intervalMinutes =
    perDay && perDay > 0 && windowHours && windowHours > 0
      ? Math.round((windowHours * 60) / perDay)
      : null;

  return (
    <div className="space-y-6">
      <div className="grid gap-4 sm:grid-cols-3">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Posts Today
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{postsToday}</div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              In Queue
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{inFlight}</div>
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
              {perDay === null ? "—" : `${perDay}/day`}
            </div>
            <p className="mt-1 text-xs text-muted-foreground">
              {intervalMinutes === null
                ? "interval not set"
                : `Every ${intervalMinutes} min`}
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
