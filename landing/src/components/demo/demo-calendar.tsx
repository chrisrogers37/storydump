"use client";

import { useMemo } from "react";
import { useSearchParams } from "next/navigation";
import { CalendarDay } from "@/components/dashboard/media/calendar-day";
import { ContentCalendar } from "@/components/dashboard/media/content-calendar";
import { useDemo } from "@/components/demo/demo-provider";
import { monthParam } from "@/lib/calendar-month";
import { demoCalendarLanes, demoDayStories, demoOpenDay } from "@/lib/demo/calendar";

/**
 * The real calendar over the sample's stories, as the visitor has left them,
 * on the sample's own clock. A day opens the real day view through `?day=`, as
 * it does in the product, so the stories a day keeps under "+N more" can be
 * read.
 */
export function DemoCalendar() {
  const { state, sample, tz } = useDemo();
  const params = useSearchParams();
  const lanes = useMemo(
    () => demoCalendarLanes(state.queue, sample.history),
    [state.queue, sample.history],
  );
  const day = demoOpenDay(params.get("day"), lanes.month);
  const stories = useMemo(
    () => (day === null ? [] : demoDayStories(state.queue, sample.history, day)),
    [state.queue, sample.history, day],
  );

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Calendar</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          What posted this month, what waits in the Queue, and what is scheduled next.
        </p>
      </div>

      <ContentCalendar {...lanes} tz={tz} navigable monthLinks={false} selected={day} />

      {day !== null && (
        <CalendarDay
          date={day}
          intents={stories}
          predicted={[]}
          predictedCut={false}
          tz={tz}
          workspaceId={null}
          closeHref={`?month=${monthParam(lanes.month)}`}
          truncatedAt={null}
        />
      )}
    </div>
  );
}
