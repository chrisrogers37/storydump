import { REVIEW_REQUIRED_STATE, type SourceRow } from "./dashboard-payloads";
import {
  connectControlFor,
  destinationConnectionCaption,
  destinationIsActive,
  destinationName,
  destinationStateBadge,
} from "./destination";
import { sourceFolderName, sourceStateLabel } from "./drive";
import type { Destination } from "./types";

/**
 * What needs the workspace's attention — the overview's condition surface.
 *
 * DERIVED, NEVER AN INBOX. Every line is a fact the database already holds (a
 * destination's state or Instagram access, a folder's sync state, a post
 * waiting on a decision), read through the payloads the tabs render and in
 * the tabs' own words. Nothing writes a notification, so nothing here can be
 * stale, unread, or disagree with the row it describes: a condition clears
 * when its row does. An inbox would need a writer, a read state and a dedup
 * policy, and could drift from the thing it reports.
 *
 * This is the pull half of the rule every producer follows — a condition is
 * never gated on a channel. Whatever was or was not pushed, it renders here.
 */

/** Where each kind of condition is resolved: the tab, and the label that says so. */
export const RESOLVED_IN = {
  accounts: { href: "/dashboard/settings?tab=accounts", action: "Open Accounts" },
  integrations: { href: "/dashboard/settings?tab=integrations", action: "Open Integrations" },
  queue: { href: "/dashboard/queue", action: "Open Queue" },
} as const;

/**
 * The all-clear, naming every kind of condition `deriveConditions` checks, so
 * it claims no more than was looked at. A new kind is a new clause here.
 */
export const ALL_CLEAR_DETAIL =
  "No account needs reconnecting, no Drive folder has stopped syncing, and no post is waiting on a decision.";

export type Condition = {
  /** Stable per row, for React's key. */
  key: string;
  /** What is wrong, naming the thing the way its tab names it. */
  text: string;
  /** The tab where it is resolved. */
  href: string;
  /** The link's label — where it goes, never an action it does not take. */
  action: string;
};

export type ConditionInputs = {
  accounts: Pick<
    Destination,
    "id" | "handle" | "display_name" | "state" | "credential_status"
  >[];
  sources: Pick<SourceRow, "id" | "state" | "folder_name">[];
  /** `stats.intents_by_state` — counted where the rows are, never a bounded list re-summed. */
  intentsByState: Record<string, number>;
};

/** Why a destination needs attention, in the Accounts tab's words — or null. */
function destinationReason(account: ConditionInputs["accounts"][number]): string | null {
  // State first: a destination the clock will not act on is the stronger
  // fact, and its badge already says why.
  if (!destinationIsActive(account.state)) {
    return destinationStateBadge(account.state).label;
  }
  // An active destination whose Instagram access died says so on its row; the
  // overview must not show an all-clear beside that. A destination posted to
  // by hand was never connected, and that is not a fault.
  if (connectControlFor(account.credential_status)?.kind === "reconnect") {
    return destinationConnectionCaption(account.credential_status);
  }
  return null;
}

export function deriveConditions({
  accounts,
  sources,
  intentsByState,
}: ConditionInputs): Condition[] {
  const conditions: Condition[] = [];

  for (const account of accounts) {
    const reason = destinationReason(account);
    if (reason === null) continue;
    conditions.push({
      key: `account:${account.id}`,
      text: `${destinationName(account)} — ${reason}`,
      ...RESOLVED_IN.accounts,
    });
  }

  // `error` is the state reserved for a failed sync, which stops the folder
  // until a reconnect or a re-pick re-arms it. A removed or disconnected
  // folder is `paused`: a decision, not a condition.
  for (const source of sources) {
    if (source.state !== "error") continue;
    conditions.push({
      key: `source:${source.id}`,
      text: `${sourceFolderName(source)} — ${sourceStateLabel(source.state)}`,
      ...RESOLVED_IN.integrations,
    });
  }

  // `review_required` is the one intent state that waits on the workspace:
  // the Queue resolves it. Every other open state is the Queue's ordinary work.
  const review = intentsByState[REVIEW_REQUIRED_STATE] ?? 0;
  if (review > 0) {
    conditions.push({
      key: REVIEW_REQUIRED_STATE,
      text: review === 1 ? "1 post needs a decision" : `${review} posts need a decision`,
      ...RESOLVED_IN.queue,
    });
  }

  return conditions;
}
