"use client";

import { Clock } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/dashboard/empty-state";
import { INTENT_STATE_TONE, TONE_CLASS } from "@/components/dashboard/tone";
import type { Intent } from "@/lib/intents";
import { formatInZone } from "@/lib/zoned-dates";

/**
 * Recent activity, from the intent ledger (#1044: a history tab is
 * `intents?state=posted,skipped,rejected`, one call).
 *
 * `entered_state_at` is when the intent reached the state shown, which is what
 * the legacy `posted_at` meant for a posted row and is the only honest reading
 * for a skipped or rejected one — those were never "posted" at any time.
 */

/**
 * The five columns this list reads, NARROWED from the intent row rather than
 * re-declared. `Pick` is the point: the component says what it needs, the
 * compiler says whether the row still has it, and a column renamed on the
 * server fails here instead of rendering `undefined`.
 */
type ActivityItem = Pick<
  Intent,
  "id" | "state" | "file_name" | "category" | "entered_state_at"
>;

const WHEN: Intl.DateTimeFormatOptions = {
  month: "short",
  day: "numeric",
  hour: "numeric",
  minute: "2-digit",
};

/**
 * When a row reached its state, read on the workspace's clock as the Queue's
 * times are (#1511). Without a zone it reads the clock the code runs on, which
 * the server (UTC) and a browser elsewhere disagree about: React rejects the
 * server's HTML (React error 418). Every page that has the workspace's zone
 * passes it.
 */
function when(iso: string, tz: string | undefined): string {
  return tz
    ? formatInZone(iso, tz, WHEN)
    : new Date(iso).toLocaleDateString("en-US", WHEN);
}

export function RecentActivity({
  items,
  tz,
}: {
  items: ActivityItem[];
  /** The workspace's zone, which every time in the list is read in (see `when`). */
  tz?: string;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Recent activity</CardTitle>
      </CardHeader>
      <CardContent>
        {items.length === 0 ? (
          <EmptyState
            icon={Clock}
            title="Quiet so far"
            description="Your Stories show up here once your schedule starts running."
            action={{ label: "Go to Settings", href: "/dashboard/settings" }}
          />
        ) : (
          <div className="space-y-3">
            {items.map((item) => (
              <div
                key={item.id}
                className="flex items-center justify-between gap-4 text-sm"
              >
                <div className="min-w-0 flex-1">
                  <p className="truncate font-medium">{item.file_name}</p>
                  <p className="text-xs text-muted-foreground">
                    {item.category ? `${item.category} · ` : ""}
                    {when(item.entered_state_at, tz)}
                  </p>
                </div>
                <Badge
                  variant="secondary"
                  className={TONE_CLASS[INTENT_STATE_TONE[item.state]]}
                >
                  {item.state}
                </Badge>
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
