"use client";

import {
  ResponsiveContainer,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  CartesianGrid,
} from "recharts";
import { BarChart3 } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/dashboard/empty-state";

/**
 * One bar per day, from the cap ledger (#1044 `stats.posts_by_day`).
 *
 * The legacy chart stacked posted/skipped/rejected per day. `daily_post_counts`
 * records what was POSTED against the capacity in force when it was written, so
 * a stack of outcomes is not what this data is — drawing one would need three
 * series the ledger does not carry. One bar and the day's cap beside it is the
 * honest reading of the same rows, and the cap is the more useful second number
 * anyway: it says whether a quiet day was quiet or full.
 */
interface DayCount {
  local_date: string;
  count: number;
  cap: number;
}

export function PostingChart({ data }: { data: DayCount[] }) {
  const formatted = data.map((d) => ({
    ...d,
    label: new Date(d.local_date).toLocaleDateString("en-US", {
      month: "short",
      day: "numeric",
    }),
  }));

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Daily Posting Activity</CardTitle>
      </CardHeader>
      <CardContent>
        {formatted.length === 0 ? (
          <EmptyState
            icon={BarChart3}
            title="No posting data yet"
            description="Chart data will appear once your first posts go out."
          />
        ) : (
          <ResponsiveContainer width="100%" height={300}>
            <BarChart data={formatted}>
              <CartesianGrid strokeDasharray="3 3" className="opacity-30" />
              <XAxis
                dataKey="label"
                tick={{ fontSize: 12 }}
                interval="preserveStartEnd"
              />
              <YAxis tick={{ fontSize: 12 }} allowDecimals={false} />
              <Tooltip
                formatter={(value, name) => [
                  value as number,
                  name === "count" ? "posted" : "capacity",
                ]}
              />
              <Bar dataKey="count" fill="hsl(142, 76%, 36%)" radius={[2, 2, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        )}
      </CardContent>
    </Card>
  );
}
