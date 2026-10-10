import {
  accountLabel,
  actionsFor,
  type Intent,
  type IntentState,
  type QueueAction,
} from "@/lib/intents";
import { SAMPLE_API_PUBLISHING } from "./fixtures";

/**
 * What a visitor does in the sample workspace, decided in the browser and
 * kept in memory only, so a reload starts the sample over (#1480).
 */

/**
 * The levers the sample answers: those a new workspace's Queue offers a story
 * waiting for a tap. Direct posting is off there, so the levers are Posted
 * myself, Skip and Reject (#1649). Nothing here posts: Posted myself records a
 * story the visitor posted by hand.
 */
export const DEMO_ACTIONS = ["mark_posted", "skip", "reject"] as const satisfies readonly QueueAction[];

export type DemoAction = (typeof DEMO_ACTIONS)[number];

/** The state each lever leaves a story in: the ledger's own. */
const STATE_AFTER: Record<DemoAction, IntentState> = {
  mark_posted: "posted",
  skip: "skipped",
  reject: "rejected",
};

export function isDemoAction(action: QueueAction): action is DemoAction {
  return (DEMO_ACTIONS as readonly QueueAction[]).includes(action);
}

/**
 * The levers a story offers: the real Queue's own matrix (`actionsFor`) for a
 * story in that state, with direct posting as the sample's workspace has it.
 * The sample's stories carry no link, no pending cancellation and no planner,
 * so the matrix's other inputs stay at their defaults.
 */
export function demoActionsFor(intent: Pick<Intent, "state">): DemoAction[] {
  return actionsFor(intent.state, SAMPLE_API_PUBLISHING).filter(isDemoAction);
}

/**
 * What the tap would have done in a real workspace, in the product's own
 * words (the Queue's Reject dialog: a skipped story comes back later, a
 * rejected one is never offered again).
 */
export function outcomeFor(
  intent: Pick<Intent, "account_handle" | "account_display_name">,
  action: DemoAction,
): string {
  const account = accountLabel(intent);
  switch (action) {
    case "mark_posted":
      return `Marked as posted. In your workspace, this would record that you posted it to ${account}'s Story yourself.`;
    case "skip":
      return "Skipped. In your workspace, this would come back later.";
    case "reject":
      return `Rejected. In your workspace, this would never be offered again for ${account}.`;
  }
}

export type DemoState = {
  /** The Queue's stories, each decided one in its new state since the tap. */
  queue: Intent[];
  /** The line under each story the visitor decided, by story id. */
  outcomes: Record<string, string>;
  /** The end panel was closed; it stays closed until a reload. */
  dismissed: boolean;
};

export type DemoEvent =
  /** `at` is when the visitor tapped: the decided story entered its state then. */
  | { type: "act"; intentId: string; action: QueueAction; at: string }
  | { type: "dismiss" };

export function initialDemoState(queue: Intent[]): DemoState {
  return { queue, outcomes: {}, dismissed: false };
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
          i.id === intent.id
            ? { ...i, state: STATE_AFTER[action], entered_state_at: event.at }
            : i,
        ),
        outcomes: { ...state.outcomes, [intent.id]: outcomeFor(intent, action) },
      };
    }
    case "dismiss":
      return state.dismissed ? state : { ...state, dismissed: true };
  }
}

/** Stories decided before the end panel shows. */
export const DECISIONS_BEFORE_END = 3;

/**
 * The end panel's moment: three stories decided. It waits for the taps, because
 * its line is about them ("That's the job: one tap per Story").
 */
export function endPanelDue(state: DemoState): boolean {
  return !state.dismissed && Object.keys(state.outcomes).length >= DECISIONS_BEFORE_END;
}
