import Link from "next/link";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { INTENT_STATE_TONE, TONE_CLASS } from "@/components/dashboard/tone";
import { mediaTile } from "@/components/dashboard/media/media-tile";
import type { PredictedSlot } from "@/lib/calendar-month";
import type { Intent } from "@/lib/intents";
import type { ThumbnailMedia } from "@/lib/thumbnails";
import { cn } from "@/lib/utils";
import { formatCalendarDate, formatInZone, instant } from "@/lib/zoned-dates";

/** Who a predicted slot posts for: the account's handle, else its name. */
function accountOf(slot: PredictedSlot): string {
  if (slot.account_handle) return `@${slot.account_handle}`;
  return slot.account_display_name ?? "An account";
}

/**
 * A story as the day view reads it: its time, its name, its state and its
 * picture's fields. Narrowed from the intent row, so the sample workspace's
 * finished stories, which carry less, are listed by the same view.
 */
export type DayStory = Pick<Intent, "id" | "state" | "schedule_slot_at" | "file_name"> &
  ThumbnailMedia;

/**
 * A row's name, sized for a phone: the row wraps, so a state that does not fit
 * drops below the name instead of squeezing it to a letter, and a long name
 * takes a second line instead of being cut, since a phone has no hover to read
 * the rest. From `sm` up the name is one line, cut with an ellipsis.
 */
const NAME = "min-w-0 grow basis-24 break-words sm:truncate";

/**
 * One day of the calendar, opened from its cell (#1634): every story the day
 * holds and every slot the cadence will open on it, in time order, each time
 * in the workspace's zone. A story shows its picture and its state. A
 * predicted slot shows neither and says it is predicted: it is a place the
 * cadence will look for a story, not a story. The grid shows a day's first
 * few names and "+N more"; this is the rest.
 */
export function CalendarDay({
  date,
  intents,
  predicted,
  tz,
  workspaceId,
  closeHref,
  truncatedAt,
  predictedCut,
}: {
  date: string;
  intents: DayStory[];
  /** The slots the cadence will open on the day (`GET …/upcoming`). */
  predicted: PredictedSlot[];
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
}) {
  // The sort is stable and the stories come first, so a story precedes a slot at its time.
  const rows: (DayStory | PredictedSlot)[] = [...intents, ...predicted].sort(
    (a, b) => instant(a.schedule_slot_at).getTime() - instant(b.schedule_slot_at).getTime()
  );
  const time = (at: string) => formatInZone(at, tz, { hour: "numeric", minute: "2-digit" });

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between pb-3">
        <CardTitle>
          {formatCalendarDate(date, { weekday: "long", month: "long", day: "numeric" })}
        </CardTitle>
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
          <ul className="divide-y">
            {rows.map((row) =>
              "kind" in row ? (
                <li
                  key={`${row.ig_account_id}@${row.schedule_slot_at}`}
                  className="flex flex-wrap items-center gap-3 py-2 text-sm"
                >
                  <div
                    className="h-10 w-10 shrink-0 rounded-md border border-dashed border-muted-foreground/40"
                    aria-hidden
                  />
                  <span className="w-20 shrink-0 tabular-nums text-muted-foreground">
                    {time(row.schedule_slot_at)}
                  </span>
                  <span className={cn(NAME, "text-muted-foreground")}>
                    {accountOf(row)}
                  </span>
                  <span
                    className={cn("shrink-0 rounded px-1.5 py-0.5 text-xs", TONE_CLASS.inert)}
                  >
                    predicted
                  </span>
                </li>
              ) : (
                <li key={row.id} className="flex flex-wrap items-center gap-3 py-2 text-sm">
                  {mediaTile({
                    media: row,
                    workspaceId,
                    box: "h-10 w-10 rounded-md",
                    glyph: "h-5 w-5",
                  })}
                  <span className="w-20 shrink-0 tabular-nums text-muted-foreground">
                    {time(row.schedule_slot_at)}
                  </span>
                  <span className={NAME} title={row.file_name}>
                    {row.file_name}
                  </span>
                  <span
                    className={cn(
                      "shrink-0 rounded px-1.5 py-0.5 text-xs",
                      TONE_CLASS[INTENT_STATE_TONE[row.state]]
                    )}
                  >
                    {row.state.replaceAll("_", " ")}
                  </span>
                </li>
              )
            )}
          </ul>
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
      </CardContent>
    </Card>
  );
}
