import {
  accountLabel,
  type Intent,
  type IntentState,
  type QueueAction,
} from "@/lib/intents";

/**
 * What a visitor does in the sample workspace, decided in the browser and
 * kept in memory only, so a reload starts the sample over (#1480).
 */

/**
 * The levers the sample offers on a story waiting on a decision. Never
 * Posted myself, and nothing that posts: nothing here may look like it
 * posted.
 */
export const DEMO_ACTIONS = ["approve", "skip", "reject"] as const satisfies readonly QueueAction[];

export type DemoAction = (typeof DEMO_ACTIONS)[number];

export const DEMO_PAGES = ["overview", "queue", "calendar"] as const;

export type DemoPage = (typeof DEMO_PAGES)[number];

/** The state each lever leaves a story in: the ledger's own. */
const STATE_AFTER: Record<DemoAction, IntentState> = {
  approve: "approved",
  skip: "skipped",
  reject: "rejected",
};

export function isDemoAction(action: QueueAction): action is DemoAction {
  return (DEMO_ACTIONS as readonly QueueAction[]).includes(action);
}

/** The levers a story offers: all three while it waits on a decision, none after. */
export function demoActionsFor(intent: Pick<Intent, "state">): DemoAction[] {
  return intent.state === "awaiting_approval" ? [...DEMO_ACTIONS] : [];
}

/**
 * What the tap would have done in a real workspace, in the product's own
 * words (the Queue's Reject dialog: a skipped story comes back later, a
 * rejected one is never offered again). A story waits here because its slot
 * has arrived, so an approved one would post right away.
 */
export function outcomeFor(
  intent: Pick<Intent, "account_handle" | "account_display_name">,
  action: DemoAction,
): string {
  const account = accountLabel(intent);
  switch (action) {
    case "approve":
      return `Approved. In your workspace, this would post to ${account}'s Story right away.`;
    case "skip":
      return "Skipped. In your workspace, this would come back later.";
    case "reject":
      return `Rejected. In your workspace, this would never be offered again for ${account}.`;
  }
}

export type DemoState = {
  queue: Intent[];
  /** The line under each story the visitor decided, by story id. */
  outcomes: Record<string, string>;
  visited: DemoPage[];
  /** The end panel was closed; it stays closed until a reload. */
  dismissed: boolean;
};

export type DemoEvent =
  | { type: "act"; intentId: string; action: QueueAction }
  | { type: "visit"; page: DemoPage }
  | { type: "dismiss" };

export function initialDemoState(queue: Intent[]): DemoState {
  return { queue, outcomes: {}, visited: [], dismissed: false };
}

export function demoReducer(state: DemoState, event: DemoEvent): DemoState {
  switch (event.type) {
    case "act": {
      const intent = state.queue.find((i) => i.id === event.intentId);
      // A story is decided once, and only with a lever it offers.
      if (
        !intent ||
        !isDemoAction(event.action) ||
        !demoActionsFor(intent).includes(event.action)
      ) {
        return state;
      }
      const action = event.action;
      return {
        ...state,
        queue: state.queue.map((i) =>
          i.id === intent.id ? { ...i, state: STATE_AFTER[action] } : i,
        ),
        outcomes: { ...state.outcomes, [intent.id]: outcomeFor(intent, action) },
      };
    }
    case "visit":
      return state.visited.includes(event.page)
        ? state
        : { ...state, visited: [...state.visited, event.page] };
    case "dismiss":
      return state.dismissed ? state : { ...state, dismissed: true };
  }
}

/** Stories decided before the end panel shows. */
export const DECISIONS_BEFORE_END = 3;

/** The end panel's moment: three stories decided, or all three pages opened. */
export function endPanelDue(state: DemoState): boolean {
  if (state.dismissed) return false;
  return (
    Object.keys(state.outcomes).length >= DECISIONS_BEFORE_END ||
    DEMO_PAGES.every((page) => state.visited.includes(page))
  );
}
