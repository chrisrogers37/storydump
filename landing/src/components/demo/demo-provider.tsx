"use client";

import {
  createContext,
  useContext,
  useMemo,
  useReducer,
  type ReactNode,
} from "react";
import type { Intent, QueueAction } from "@/lib/intents";
import type { SampleWorkspace } from "@/lib/demo/fixtures";
import { demoReducer, initialDemoState, type DemoState } from "@/lib/demo/state";

type DemoActions = {
  /** Every decision a visitor makes lands here, and only here. */
  act: (intent: Intent, action: QueueAction) => void;
  dismiss: () => void;
};

type DemoContextValue = DemoActions & {
  state: DemoState;
  /** The sample as the server built it for this visit, before any tap. */
  sample: SampleWorkspace;
  tz: string;
};

const DemoContext = createContext<DemoContextValue | null>(null);

/**
 * The sample workspace's state, held by its layout (#1480). A layout survives
 * navigation between the pages beneath it, so a decision in the Queue reaches
 * the Calendar and the Overview; it lives in memory only, so a reload starts
 * the sample over.
 */
export function DemoProvider({
  sample,
  children,
}: {
  sample: SampleWorkspace;
  children: ReactNode;
}) {
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

export function useDemo(): DemoContextValue {
  const value = useContext(DemoContext);
  if (value === null) {
    throw new Error("useDemo must be used inside DemoProvider");
  }
  return value;
}
