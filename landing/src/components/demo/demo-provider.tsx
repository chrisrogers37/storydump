"use client";

import {
  createContext,
  useContext,
  useMemo,
  useReducer,
  type ReactNode,
} from "react";
import type { Intent, QueueAction } from "@/lib/intents";
import {
  demoReducer,
  initialDemoState,
  type DemoPage,
  type DemoState,
} from "@/lib/demo/state";

type DemoActions = {
  /** Every decision a visitor makes lands here, and only here. */
  act: (intent: Intent, action: QueueAction) => void;
  visit: (page: DemoPage) => void;
  dismiss: () => void;
};

type DemoContextValue = DemoActions & {
  state: DemoState;
  history: Intent[];
  tz: string;
};

const DemoContext = createContext<DemoContextValue | null>(null);

/**
 * The sample workspace's state, held by its layout (#1480). A layout survives
 * navigation between the pages beneath it, so a decision in the Queue reaches
 * the Calendar; it lives in memory only, so a reload starts the sample over.
 */
export function DemoProvider({
  queue,
  history,
  tz,
  children,
}: {
  queue: Intent[];
  history: Intent[];
  tz: string;
  children: ReactNode;
}) {
  const [state, dispatch] = useReducer(demoReducer, queue, initialDemoState);

  const actions = useMemo<DemoActions>(
    () => ({
      act: (intent, action) => dispatch({ type: "act", intentId: intent.id, action }),
      visit: (page) => dispatch({ type: "visit", page }),
      dismiss: () => dispatch({ type: "dismiss" }),
    }),
    [],
  );

  const value = useMemo(
    () => ({ ...actions, state, history, tz }),
    [actions, state, history, tz],
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
