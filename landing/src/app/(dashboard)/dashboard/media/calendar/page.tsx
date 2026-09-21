import { requireWorkspacePage } from "@/lib/page-guards";
import { workspaceFetch } from "@/lib/workspaces";
import {
  HISTORY_STATES,
  QUEUE_STATES,
  REVIEW_REQUIRED_STATE,
  SCHEDULED_STATES,
  type StatsResponse,
  type WorkspaceConfig,
} from "@/lib/dashboard-payloads";
import type { Intent, IntentsResponse } from "@/lib/intents";
import { postingIntervalMinutes } from "@/lib/schedule";
import { RouterUnavailable } from "@/components/workspace/router-unavailable";
import { ContentCalendar } from "@/components/dashboard/media/content-calendar";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

/** The calendar's lanes are all the intent ledger now, filtered by state. */
const laneItem = (i: Intent) => ({
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

/**
 * The calendar's three bounded reads (`01` H5). History and the schedule
 * strip are drawn as dots on a month, so fifteen is what fits; the queue
 * lane lists rows, so it is the shorter ten. Every COUNT on this page comes
 * from `stats`, never from these lists.
 */
const CALENDAR_HISTORY_LIMIT = 15;
const CALENDAR_QUEUE_LIMIT = 10;
const CALENDAR_SCHEDULE_LIMIT = 15;

export default async function CalendarPage() {
  const { workspaceId } = await requireWorkspacePage();

  const [historyResult, queueResult, scheduleResult, statsResult, configResult] =
    await Promise.all([
      workspaceFetch<IntentsResponse>(
        `intents?state=${HISTORY_STATES}&limit=${CALENDAR_HISTORY_LIMIT}`,
        workspaceId,
      ),
      workspaceFetch<IntentsResponse>(
        `intents?state=${QUEUE_STATES}&limit=${CALENDAR_QUEUE_LIMIT}`,
        workspaceId,
      ),
      workspaceFetch<IntentsResponse>(
        `intents?state=${SCHEDULED_STATES}&limit=${CALENDAR_SCHEDULE_LIMIT}`,
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
  // Counted in the total above AND named here. Surfaced only above zero: a
  // permanent "0 need review" line is noise on every healthy workspace, and
  // the operator action it points at does not exist at zero.
  const needsReview = stats.intents_by_state?.[REVIEW_REQUIRED_STATE] ?? 0;

  // Derived from the workspace's own config rather than served: the posting
  // window divided by the daily target. Null config means no answer, not zero.
  //
  // Through `schedule.ts`, which mirrors `fn_next_slot` — the function that
  // actually decides when posts go out. This page used to do the arithmetic
  // itself as a bare `end - start` guarded on `> 0`, and so had no answer for
  // the two shapes that function handles: a window that wraps midnight reads
  // negative, and a 24-hour window reads zero. Both fell through to "interval
  // not set", and 14 → 2 is the schema default (#1367).
  const perDay = config.posts_per_day;
  const intervalMinutes = postingIntervalMinutes(
    config.posting_hours_start,
    config.posting_hours_end,
    perDay,
  );

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
            {needsReview > 0 && (
              <p className="mt-1 text-xs text-muted-foreground">
                {needsReview === 1
                  ? "1 needs review"
                  : `${needsReview} need review`}
              </p>
            )}
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
