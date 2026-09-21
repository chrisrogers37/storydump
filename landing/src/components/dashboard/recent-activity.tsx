"use client";

import { Clock } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/dashboard/empty-state";
import type { Intent } from "@/lib/intents";

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

const statusVariant: Record<string, string> = {
  posted: "bg-green-100 text-green-800",
  skipped: "bg-yellow-100 text-yellow-800",
  rejected: "bg-red-100 text-red-800",
  failed: "bg-red-100 text-red-800",
};

export function RecentActivity({ items }: { items: ActivityItem[] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Recent Activity</CardTitle>
      </CardHeader>
      <CardContent>
        {items.length === 0 ? (
          <EmptyState
            icon={Clock}
            title="No activity yet"
            description="Posts will appear here once your scheduler starts running."
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
                  <p className="text-xs text-muted-foreground capitalize">
                    {item.category ? `${item.category} · ` : ""}
                    {new Date(item.entered_state_at).toLocaleDateString("en-US", {
                      month: "short",
                      day: "numeric",
                      hour: "numeric",
                      minute: "2-digit",
                    })}
                  </p>
                </div>
                <Badge
                  variant="secondary"
                  className={statusVariant[item.state] || ""}
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
