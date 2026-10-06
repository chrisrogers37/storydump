"use client";

import { QueueView } from "@/components/dashboard/queue/queue-view";
import { useDemo } from "@/components/demo/demo-provider";
import { demoActionsFor } from "@/lib/demo/state";

/**
 * The real Queue's rows over the sample's stories. Approve, Skip and Reject
 * decide in the browser; a decided row keeps its place with its new badge and
 * a line saying what the tap would have done in a real workspace.
 */
export function DemoQueue() {
  const { state, tz, act } = useDemo();

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Queue</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Every post that is not done yet, in slot order. Times are in {tz}.
        </p>
      </div>

      <QueueView
        intents={state.queue}
        tz={tz}
        truncatedAt={null}
        pending={null}
        actionsOf={demoActionsFor}
        noteFor={(intent) => {
          const text = state.outcomes[intent.id];
          return text ? { text, tone: "status" } : null;
        }}
        onAction={act}
      />
    </div>
  );
}
