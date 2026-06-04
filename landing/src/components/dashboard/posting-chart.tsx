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

interface DailyCount {
  date: string;
  posted: number;
  skipped: number;
  rejected: number;
  failed?: number;
}

export function PostingChart({ data }: { data: DailyCount[] }) {
  // Format date labels to be shorter
  const formatted = data.map((d) => ({
    ...d,
    label: new Date(d.date).toLocaleDateString("en-US", {
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
              <Tooltip />
              <Bar
                dataKey="posted"
                stackId="a"
                fill="hsl(142, 76%, 36%)"
                name="Posted"
                radius={[0, 0, 0, 0]}
              />
              <Bar
                dataKey="skipped"
                stackId="a"
                fill="hsl(48, 96%, 53%)"
                name="Skipped"
                radius={[0, 0, 0, 0]}
              />
              <Bar
                dataKey="rejected"
                stackId="a"
                fill="hsl(0, 84%, 60%)"
                name="Rejected"
                radius={[0, 0, 0, 0]}
              />
              <Bar
                dataKey="failed"
                stackId="a"
                fill="hsl(0, 70%, 35%)"
                name="Failed"
                radius={[4, 4, 0, 0]}
              />
            </BarChart>
          </ResponsiveContainer>
        )}
      </CardContent>
    </Card>
  );
}
