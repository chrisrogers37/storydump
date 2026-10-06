"use client";

import { useMemo } from "react";
import { ContentCalendar } from "@/components/dashboard/media/content-calendar";
import { useDemo } from "@/components/demo/demo-provider";
import { demoCalendarLanes } from "@/lib/demo/calendar";

/** The real calendar over the sample's stories, as the visitor has left them. */
export function DemoCalendar() {
  const { state, history } = useDemo();
  const lanes = useMemo(
    () => demoCalendarLanes(state.queue, history),
    [state.queue, history],
  );

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Calendar</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          What posted, what is queued and what is predicted, this month.
        </p>
      </div>

      <ContentCalendar {...lanes} />
    </div>
  );
}
