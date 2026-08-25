/**
 * The web queue's contract with the ledger, and its pure decisions.
 *
 * `Intent` is the row `GET /api/v1/workspaces/{ws}/intents` returns — the
 * `02` §4 intent plus the media it posts and the account it posts to. The
 * decisions beside it (which buttons a row gets, what a refusal says, how a
 * slot reads) are kept out of the components so they can be pinned without a
 * DOM: a wrong answer here is a button that answers 409, or no button on the
 * one state a person can act on.
 *
 * WHY THESE FOUR COMMANDS AND NOT THE VOCABULARY. The matrix admits a human
 * lever from `awaiting_approval` only: Approve (`approve`, and only where the
 * workspace can publish by API — otherwise the port refuses `manual_mode`),
 * Posted myself (`mark_posted`, the manual-mode path), Skip and Reject.
 * `cancel` sets an overlay flag the worker honours with no audit row of its
 * own, and `autopost_now` is unbuilt (501); each is a follow-up with its own
 * semantics, not a missing entry in this list.
 */

export const INTENT_STATES = [
  "scheduled",
  "prompt_pending",
  "awaiting_approval",
  "approved",
  "publishing",
  "publishing_ambiguous",
  "review_required",
  "posted",
  "skipped",
  "rejected",
  "expired",
  "failed",
  "cancelled",
] as const;

export type IntentState = (typeof INTENT_STATES)[number];

/** The states the queue lists: everything the reaper or worker has not yet closed. */
export const NON_TERMINAL_STATES: readonly IntentState[] = [
  "scheduled",
  "prompt_pending",
  "awaiting_approval",
  "approved",
  "publishing",
  "publishing_ambiguous",
  "review_required",
];

export type Intent = {
  id: string;
  state: IntentState;
  ig_account_id: string;
  media_item_id: string;
  /** ISO 8601, as Postgres renders a timestamptz — fractional seconds of any width. */
  schedule_slot_at: string;
  approval_mode: "manual" | "auto";
  published_via: string | null;
  publish_step: string | null;
  cancel_requested: boolean;
  ig_permalink: string | null;
  entered_state_at: string;
  created_at: string;
  file_name: string;
  media_kind: string;
  thumbnail_url: string | null;
  caption: string | null;
  category: string | null;
  /** NULL when the account carries no handle — the key is always present. */
  account_handle: string | null;
  account_display_name: string | null;
};

export type IntentsResponse = { intents: Intent[]; limit: number };

export const QUEUE_COMMANDS = ["approve", "mark_posted", "skip", "reject"] as const;

export type QueueCommand = (typeof QUEUE_COMMANDS)[number];

export const COMMAND_LABELS: Record<QueueCommand, string> = {
  approve: "Approve",
  mark_posted: "Posted myself",
  skip: "Skip",
  reject: "Reject",
};

/** The id the browser sends: a UUID or nothing — never a free-form string forwarded into a command. */
export function isIntentId(value: unknown): value is string {
  return (
    typeof value === "string" &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)
  );
}

export function isQueueCommand(value: unknown): value is QueueCommand {
  return (
    typeof value === "string" && (QUEUE_COMMANDS as readonly string[]).includes(value)
  );
}

/**
 * The buttons a row gets. Only `awaiting_approval` has a human lever in the
 * matrix; hybrid (`api_publishing_enabled`) keeps the manual buttons beside
 * Approve (`06` §3). Every other state renders read-only with its badge.
 */
export function actionsFor(
  state: IntentState,
  apiPublishingEnabled: boolean,
): QueueCommand[] {
  if (state !== "awaiting_approval") return [];
  return apiPublishingEnabled
    ? ["approve", "mark_posted", "skip", "reject"]
    : ["mark_posted", "skip", "reject"];
}

/**
 * Stable per (command, intent): a double-click is a `200 replayed`, never a
 * second execution, and a refused command — whose dedup row rolled back with
 * everything else — stays retryable under the same key. The API caps the
 * header at 200 characters; a command name plus a UUID is well under.
 */
export function idempotencyKeyFor(command: QueueCommand, intentId: string): string {
  return `${command}:${intentId}`;
}

/**
 * A sentence per refusal, because the matrix's 409s are normal answers, not
 * errors: "this post already moved on" and "this workspace posts by hand" send
 * a person to different next moves, and neither is theirs to apologise for.
 */
export function refusalCopy(reason: string, status: number): string {
  switch (reason) {
    case "illegal_transition":
      return "This post already moved on — the list has been refreshed.";
    case "manual_mode":
      return "This workspace posts by hand. Post the story on Instagram, then tap Posted myself.";
    case "not_found":
      return "This post is no longer in the queue.";
  }
  if (status === 401 || reason === "unauthenticated") {
    return "That session expired. Sign in again.";
  }
  if (status === 503 || reason === "target_router_unreachable") {
    return "Storydump cannot reach the queue right now. Nothing changed — try again shortly.";
  }
  return "That did not work. Nothing changed — try again shortly.";
}

/** The handle a person recognises the row by; the display name when there is none. */
export function accountLabel(
  intent: Pick<Intent, "account_handle" | "account_display_name">,
): string {
  const handle = intent.account_handle?.trim();
  if (handle) return handle;
  const name = intent.account_display_name?.trim();
  if (name) return name;
  return "Account";
}

/**
 * The slot in the WORKSPACE's clock — a solo user reads their own time, never
 * UTC. Postgres renders fractional seconds at any width and `Date` only
 * promises three, so the fraction is trimmed first; a time zone Intl does not
 * know (the column is CHECKed, so this is belt and braces) falls back to UTC
 * and says so rather than throwing inside a list render.
 */
export function formatSlot(iso: string, tz: string): string {
  const date = new Date(iso.replace(/\.(\d{3})\d+/, ".$1"));
  const options: Intl.DateTimeFormatOptions = {
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  };
  try {
    return new Intl.DateTimeFormat("en-US", { ...options, timeZone: tz }).format(date);
  } catch {
    return `${new Intl.DateTimeFormat("en-US", { ...options, timeZone: "UTC" }).format(date)} UTC`;
  }
}
