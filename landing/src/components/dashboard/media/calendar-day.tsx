import Link from "next/link";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { INTENT_STATE_TONE, TONE_CLASS } from "@/components/dashboard/tone";
import { mediaTile } from "@/components/dashboard/media/media-tile";
import { OpenedDayTitle } from "@/components/dashboard/media/opened-day";
import { RepeatMark } from "@/components/dashboard/repeat-mark";
import { withPastPostsName } from "@/lib/past-posts-media";
import type { PredictedSlot, RepeatOccurrence } from "@/lib/calendar-month";
import { isLive, plannedStoryHref, type Intent } from "@/lib/intents";
import type { RepeatRule } from "@/lib/repeat";
import type { ThumbnailMedia } from "@/lib/thumbnails";
import { cn } from "@/lib/utils";
import { formatCalendarDate, formatInZone, instant } from "@/lib/zoned-dates";

/** Who a predicted slot posts for: the account's handle, else its name. */
function accountOf(slot: PredictedSlot): string {
  if (slot.account_handle) return `@${slot.account_handle}`;
  return slot.account_display_name ?? "An account";
}

/**
 * A story as the day view reads it: its time, its name, its state, its
 * picture's fields, and how it repeats when it does, in its own zone (#485),
 * marked while it lives: an ended story keeps its rule, but says nothing of a
 * series that may have stopped since. Narrowed from the intent row, so the
 * sample workspace's finished stories, which carry less, are listed by the
 * same view.
 */
export type DayStory = Pick<Intent, "id" | "state" | "schedule_slot_at" | "file_name"> &
  Partial<Pick<Intent, "repeat" | "tz">> &
  ThumbnailMedia;

/**
 * A row's name, sized for a phone: the row wraps, so a state that does not fit
 * drops below the name instead of squeezing it to a letter, and a long name
 * takes a second line instead of being cut, since a phone has no hover to read
 * the rest. From `sm` up the name is one line, cut with an ellipsis.
 */
const NAME = "min-w-0 grow basis-24 break-words sm:truncate";

/** A row, and the badge that ends it: a story's state, or what a place is. */
const ROW = "flex flex-wrap items-center gap-3 py-2 text-sm";
const BADGE = "shrink-0 rounded px-1.5 py-0.5 text-xs";

/**
 * One day of the calendar, opened from its cell (#1634): every story the day
 * holds and every slot the cadence will open on it, in time order, each time
 * in the workspace's zone. A story shows its picture and its state. A
 * predicted slot shows neither and says it is predicted: it is a place the
 * cadence will look for a story, not a story. A repeating series' later
 * occurrence (#485) is a place too: it shows its picture, lighter, and says it
 * repeats, and its name opens its series in the Queue. The grid shows a day's
 * first few names and "+N more"; this is the rest.
 *
 * It is drawn after the month, so its title brings it on screen when it opens
 * and hands the visitor back to the day when it closes (`OpenedDayTitle`).
 */
export function CalendarDay({
  date,
  intents,
  predicted,
  repeating,
  tz,
  workspaceId,
  closeHref,
  truncatedAt,
  predictedCut,
  repeatingCut,
}: {
  date: string;
  intents: DayStory[];
  /** The slots the cadence will open on the day (`GET …/upcoming`). */
  predicted: PredictedSlot[];
  /** The repeating series' later occurrences on the day (#485), from the same read. */
  repeating: RepeatOccurrence[];
  tz: string;
  /**
   * The workspace whose thumbnails the rows ask for. The sample workspace has
   * none, so its rows draw the glyph.
   */
  workspaceId: string | null;
  /** Back to the month, with no day open. */
  closeHref: string;
  /** The read's limit when the day reached it, so a page is not read as the whole. */
  truncatedAt: number | null;
  /** The predicted slots were cut on or before this day, so it may hold more of them. */
  predictedCut: boolean;
  /** The later occurrences were cut on or before this day (#485). */
  repeatingCut: boolean;
}) {
  const time = (at: string) => formatInZone(at, tz, { hour: "numeric", minute: "2-digit" });
  const when = (at: string) => (
    <span className="w-20 shrink-0 tabular-nums text-muted-foreground">{time(at)}</span>
  );
  // A rule's time is its story's own: the mark names that zone where the page reads another.
  const mark = (rule: RepeatRule, zone: string | undefined) => (
    <RepeatMark rule={rule} zone={zone !== tz ? zone : undefined} />
  );

  const storyRow = (row: DayStory) => (
    <li key={row.id} className={ROW}>
      {mediaTile({ media: row, workspaceId, box: "h-10 w-10 rounded-md", glyph: "h-5 w-5" })}
      {when(row.schedule_slot_at)}
      <span className={NAME} title={row.file_name}>
        {row.file_name}
      </span>
      {row.repeat && isLive(row.state) && mark(row.repeat, row.tz)}
      <span className={cn(BADGE, TONE_CLASS[INTENT_STATE_TONE[row.state]])}>
        {row.state.replaceAll("_", " ")}
      </span>
    </li>
  );

  const repeatRow = (row: RepeatOccurrence) => (
    <li key={`${row.intent_id}@${row.schedule_slot_at}`} className={ROW}>
      {mediaTile({
        media: row,
        workspaceId,
        box: "h-10 w-10 rounded-md opacity-60",
        glyph: "h-5 w-5",
      })}
      {when(row.schedule_slot_at)}
      <Link
        href={plannedStoryHref(row.media_item_id, row.ig_account_id)}
        className={cn(NAME, "text-muted-foreground hover:text-foreground hover:underline")}
        title={row.file_name}
      >
        {row.file_name}
        <span className="sr-only">: open its series in the Queue</span>
      </Link>
      {mark(row.repeat, row.tz)}
      <span className={cn(BADGE, TONE_CLASS.inert)}>repeats</span>
    </li>
  );

  const slotRow = (row: PredictedSlot) => (
    <li key={`${row.ig_account_id}@${row.schedule_slot_at}`} className={ROW}>
      <div
        className="h-10 w-10 shrink-0 rounded-md border border-dashed border-muted-foreground/40"
        aria-hidden
      />
      {when(row.schedule_slot_at)}
      <span className={cn(NAME, "text-muted-foreground")}>{accountOf(row)}</span>
      <span className={cn(BADGE, TONE_CLASS.inert)}>predicted</span>
    </li>
  );

  // Each list's rows by their times. The sort is stable and the stories come
  // first, so a story precedes a place at its time. A past-posts item goes by its
  // own name; the API sends it, and `withPastPostsName` is the guard for a row
  // that arrives without it.
  const rows = [
    ...intents.map((row) => [row.schedule_slot_at, storyRow(withPastPostsName(row))] as const),
    ...repeating.map((row) => [row.schedule_slot_at, repeatRow(withPastPostsName(row))] as const),
    ...predicted.map((row) => [row.schedule_slot_at, slotRow(row)] as const),
  ].sort(([a], [b]) => instant(a).getTime() - instant(b).getTime());

  return (
    // The margin is the page's own padding, so the view comes to rest clear of the edge.
    <Card className="scroll-my-6">
      <CardHeader className="flex-row items-center justify-between pb-3">
        <OpenedDayTitle date={date}>
          {formatCalendarDate(date, { weekday: "long", month: "long", day: "numeric" })}
        </OpenedDayTitle>
        <Link
          href={closeHref}
          scroll={false}
          className="text-sm text-muted-foreground hover:text-foreground hover:underline"
        >
          Close
        </Link>
      </CardHeader>
      <CardContent>
        {rows.length === 0 ? (
          <p className="text-sm text-muted-foreground">No stories on this day.</p>
        ) : (
          <ul className="divide-y">{rows.map(([, row]) => row)}</ul>
        )}
        {truncatedAt !== null && (
          <p className="mt-2 text-xs text-muted-foreground">
            Showing the first {truncatedAt} stories of this day.
          </p>
        )}
        {predictedCut && (
          <p className="mt-2 text-xs text-muted-foreground">
            This day may hold more predicted slots than are shown.
          </p>
        )}
        {repeatingCut && (
          <p className="mt-2 text-xs text-muted-foreground">
            This day may hold more repeats than are shown.
          </p>
        )}
      </CardContent>
    </Card>
  );
}
