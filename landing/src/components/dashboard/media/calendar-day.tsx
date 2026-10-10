import Link from "next/link";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { INTENT_STATE_TONE, TONE_CLASS } from "@/components/dashboard/tone";
import type { Intent } from "@/lib/intents";
import { cn } from "@/lib/utils";
import { formatCalendarDate, formatInZone } from "@/lib/zoned-dates";

/**
 * One day of the calendar, opened from its cell (#1634): every story the day
 * holds, in time order, with its time in the workspace's zone and its state.
 * The grid shows a day's first few names and "+N more"; this is the rest.
 */
export function CalendarDay({
  date,
  intents,
  tz,
  closeHref,
  truncatedAt,
}: {
  date: string;
  intents: Intent[];
  tz: string;
  /** Back to the month, with no day open. */
  closeHref: string;
  /** The read's limit when the day reached it, so a page is not read as the whole. */
  truncatedAt: number | null;
}) {
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
        {intents.length === 0 ? (
          <p className="text-sm text-muted-foreground">No stories on this day.</p>
        ) : (
          <ul className="divide-y">
            {intents.map((intent) => (
              <li key={intent.id} className="flex items-center gap-3 py-2 text-sm">
                <span className="w-20 shrink-0 tabular-nums text-muted-foreground">
                  {formatInZone(intent.schedule_slot_at, tz, {
                    hour: "numeric",
                    minute: "2-digit",
                  })}
                </span>
                <span className="min-w-0 grow truncate" title={intent.file_name}>
                  {intent.file_name}
                </span>
                <span
                  className={cn(
                    "shrink-0 rounded px-1.5 py-0.5 text-xs",
                    TONE_CLASS[INTENT_STATE_TONE[intent.state]]
                  )}
                >
                  {intent.state.replaceAll("_", " ")}
                </span>
              </li>
            ))}
          </ul>
        )}
        {truncatedAt !== null && (
          <p className="mt-2 text-xs text-muted-foreground">
            Showing the first {truncatedAt} stories of this day.
          </p>
        )}
      </CardContent>
    </Card>
  );
}
