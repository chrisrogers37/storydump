import { requireWorkspacePage } from "@/lib/page-guards";
import { workspaceFetch } from "@/lib/workspaces";
import {
  POSTED_STATES,
  QUEUE_STATES,
  REVIEW_REQUIRED_STATE,
  SCHEDULED_STATES,
  type StatsResponse,
  type WorkspaceConfig,
} from "@/lib/dashboard-payloads";
import { LIST_LIMIT_MAX, type Intent, type IntentsResponse } from "@/lib/intents";
import {
  addDays,
  monthGrid,
  monthOf,
  monthParam,
  NAMES_PER_DAY,
  parseDay,
  parseMonth,
  type IntentDaysResponse,
} from "@/lib/calendar-month";
import { postingIntervalMinutes } from "@/lib/schedule";
import { dateInZone } from "@/lib/zoned-dates";
import { RouterUnavailable } from "@/components/workspace/router-unavailable";
import { CalendarDay } from "@/components/dashboard/media/calendar-day";
import { ContentCalendar } from "@/components/dashboard/media/content-calendar";
import { StatCard } from "@/components/ui/card";

/** The calendar's lanes are all the intent ledger now, filtered by state. */
const laneItem = (i: Intent) => ({
  media_name: i.file_name,
  category: i.category ?? "uncategorised",
  status: i.state,
});

/**
 * The calendar's bounded reads (`01` H5). Every COUNT on this page is one the
 * API counted: `stats`, or the month read's count per day. None is a list's
 * length.
 *
 * The month on screen is one read (#1634): every local day on its grid with
 * how many stories it posted and the newest names (`intents/days`), so a day
 * early in a busy month is counted as fully as yesterday. A day opened from
 * its cell is one more read: that day's stories, every state, in time order.
 */
const CALENDAR_QUEUE_LIMIT = 10;
const CALENDAR_SCHEDULE_LIMIT = 15;

export default async function CalendarPage({
  searchParams,
}: {
  searchParams: Promise<{ month?: string; day?: string }>;
}) {
  const { workspaceId } = await requireWorkspacePage();
  const params = await searchParams;

  // Which month to draw depends on today in the WORKSPACE's zone, so its
  // config is read first.
  const configResult = await workspaceFetch<WorkspaceConfig>("", workspaceId);
  if (!configResult.ok) {
    return <RouterUnavailable what="Your calendar" />;
  }
  const config = configResult.data;
  const tz = config.tz ?? "UTC";
  const today = dateInZone(new Date(), tz);

  const asked = parseDay(params.day);
  const month = parseMonth(params.month) ?? monthOf(asked ?? today);
  const grid = monthGrid(month);
  // A day off the grid is not this month's to open.
  const day = asked !== null && grid.dates.includes(asked) ? asked : null;

  const [historyResult, queueResult, plannedResult, scheduleResult, statsResult, dayResult] =
    await Promise.all([
      workspaceFetch<IntentDaysResponse>(
        `intents/days?state=${POSTED_STATES}&from=${grid.from}&to=${grid.to}&per_day=${NAMES_PER_DAY}`,
        workspaceId,
      ),
      // The upcoming stories are split by origin, so each is in one read: the
      // slot plan's soonest ten, and every story a person planned, which the
      // ten can then never push off the month.
      workspaceFetch<IntentsResponse>(
        `intents?state=${QUEUE_STATES}&origin=cadence&limit=${CALENDAR_QUEUE_LIMIT}`,
        workspaceId,
      ),
      workspaceFetch<IntentsResponse>(
        `intents?state=${QUEUE_STATES}&origin=planned&limit=${LIST_LIMIT_MAX}`,
        workspaceId,
      ),
      // The predicted strip is the slot plan's: a story a person planned is
      // not a prediction, and is drawn in the queue lane as planned (#1413).
      workspaceFetch<IntentsResponse>(
        `intents?state=${SCHEDULED_STATES}&origin=cadence&limit=${CALENDAR_SCHEDULE_LIMIT}`,
        workspaceId,
      ),
      workspaceFetch<StatsResponse>("stats", workspaceId),
      day === null
        ? null
        : workspaceFetch<IntentsResponse>(
            `intents?from=${day}&to=${addDays(day, 1)}&order=asc&limit=${LIST_LIMIT_MAX}`,
            workspaceId,
          ),
    ]);

  // The calendar is these side by side. One missing leaves a column of zeros
  // next to real data, which reads as "nothing scheduled" rather than as "we
  // could not ask".
  if (
    !historyResult.ok ||
    !queueResult.ok ||
    !plannedResult.ok ||
    !scheduleResult.ok ||
    !statsResult.ok ||
    (dayResult !== null && !dayResult.ok)
  ) {
    return <RouterUnavailable what="Your calendar" />;
  }

  const stats = statsResult.data;

  const queueItems = [
    ...(queueResult.data.intents ?? []),
    ...(plannedResult.data.intents ?? []),
  ].map((i) => ({
    ...laneItem(i),
    scheduled_for: i.schedule_slot_at,
    planned: i.origin === "planned",
  }));

  const scheduleSlots = (scheduleResult.data.intents ?? []).map((i) => ({
    slot_time: i.schedule_slot_at,
    predicted_category: i.category,
  }));

  // Counted where the rows are, not re-summed from the bounded lists above.
  // Today is the WORKSPACE's: `daily_post_counts.local_date` is its own date.
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

  const dayIntents = dayResult?.ok ? (dayResult.data.intents ?? []) : [];

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3">
        <StatCard label="Posts Today" value={postsToday} />
        <StatCard
          label="In Queue"
          value={inFlight}
          detail={
            needsReview > 0 &&
            (needsReview === 1 ? "1 needs review" : `${needsReview} need review`)
          }
        />
        <StatCard
          label="Posting Rate"
          value={perDay === null ? "—" : `${perDay}/day`}
          detail={
            intervalMinutes === null
              ? "interval not set"
              : `Every ${intervalMinutes} min`
          }
        />
      </div>

      <ContentCalendar
        month={month}
        history={historyResult.data.days ?? []}
        queue={queueItems}
        schedule={scheduleSlots}
        tz={tz}
        navigable
        selected={day}
      />

      {day !== null && dayResult?.ok && (
        <CalendarDay
          date={day}
          intents={dayIntents}
          tz={tz}
          closeHref={`?month=${monthParam(month)}`}
          truncatedAt={
            dayIntents.length >= dayResult.data.limit ? dayResult.data.limit : null
          }
        />
      )}
    </div>
  );
}
