"use client";

import {
  createContext,
  useContext,
  useMemo,
  useReducer,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react";
import { Skeleton } from "@/components/ui/skeleton";
import type { Intent, QueueAction } from "@/lib/intents";
import { sampleNow, type SampleWorkspace } from "@/lib/demo/fixtures";
import { demoReducer, initialDemoState, type DemoState } from "@/lib/demo/state";

type DemoActions = {
  /** Every decision a visitor makes lands here, and only here. */
  act: (intent: Intent, action: QueueAction) => void;
  dismiss: () => void;
};

type DemoContextValue = DemoActions & {
  state: DemoState;
  /** The sample as it was built when this visit opened it, before any tap. */
  sample: SampleWorkspace;
  tz: string;
};

const DemoContext = createContext<DemoContextValue | null>(null);

/** Nothing to subscribe to: the answer differs only between the server and the browser. */
const never = () => () => {};

/**
 * The sample workspace's state, held by its layout (#1480). A layout survives
 * navigation between the pages beneath it, so a decision in the Queue reaches
 * the Calendar and the Overview; it lives in memory only, so a reload starts
 * the sample over.
 *
 * BUILT IN THE VISITOR'S BROWSER (#1649). The sample is counted from now, so
 * it has no form that can be made ahead of the visit: the prerendered page,
 * and the browser while it takes that page over, draw a placeholder. Once the
 * page is the browser's, the sample is built from the visitor's own clock, the
 * clock their taps are timed by.
 */
export function DemoProvider({ children }: { children: ReactNode }) {
  const inBrowser = useSyncExternalStore(
    never,
    () => true,
    () => false,
  );
  return inBrowser ? <DemoSession>{children}</DemoSession> : <DemoPlaceholder />;
}

/**
 * One visit to the sample: its stories, and what the visitor has done with
 * them. Exported so the pages can be rendered over a built sample in tests,
 * where no browser takes the page over.
 */
export function DemoSession({ children }: { children: ReactNode }) {
  const [sample] = useState(sampleNow);
  const [state, dispatch] = useReducer(demoReducer, sample.queue, initialDemoState);

  const actions = useMemo<DemoActions>(
    () => ({
      act: (intent, action) =>
        dispatch({ type: "act", intentId: intent.id, action, at: new Date().toISOString() }),
      dismiss: () => dispatch({ type: "dismiss" }),
    }),
    [],
  );

  const value = useMemo(
    () => ({ ...actions, state, sample, tz: sample.config.tz ?? "UTC" }),
    [actions, state, sample],
  );

  return <DemoContext.Provider value={value}>{children}</DemoContext.Provider>;
}

/** A page's title, its line and its content, before there is a sample to draw. */
function DemoPlaceholder() {
  return (
    <div role="status" aria-label="Loading the sample workspace" className="space-y-6">
      <div className="space-y-2">
        <Skeleton className="h-8 w-40" />
        <Skeleton className="h-4 w-72 max-w-full" />
      </div>
      <Skeleton className="h-64 w-full rounded-lg" />
    </div>
  );
}

export function useDemo(): DemoContextValue {
  const value = useContext(DemoContext);
  if (value === null) {
    throw new Error("useDemo must be used inside DemoProvider");
  }
  return value;
}
