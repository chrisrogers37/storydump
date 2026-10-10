"use client";

import { useMemo } from "react";
import { ContentCalendar } from "@/components/dashboard/media/content-calendar";
import { useDemo } from "@/components/demo/demo-provider";
import { demoCalendarLanes } from "@/lib/demo/calendar";

/** The real calendar over the sample's stories, as the visitor has left them, on the sample's own clock. */
export function DemoCalendar() {
  const { state, sample, tz } = useDemo();
  const lanes = useMemo(
    () => demoCalendarLanes(state.queue, sample.history),
    [state.queue, sample.history],
  );

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Calendar</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          What posted this month, what waits in the Queue, and what is scheduled next.
        </p>
      </div>

      <ContentCalendar {...lanes} tz={tz} />
    </div>
  );
}
