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

/** The states the queue lists: everything the reaper or worker has not yet closed. */
export const NON_TERMINAL_STATES = [
  "scheduled",
  "prompt_pending",
  "awaiting_approval",
  "approved",
  "publishing",
  "publishing_ambiguous",
  "review_required",
] as const;

/** Mirrors `command_executors.TERMINAL_STATES` (Python) — the reaper's and worker's edges end here. */
export const TERMINAL_STATES = [
  "posted",
  "skipped",
  "rejected",
  "expired",
  "failed",
  "cancelled",
] as const;

/** `ck_intent_state`, the closed set — mirrors `workspaces.INTENT_STATES` (Python); this comment is the grep handle from either side. */
export const INTENT_STATES = [...NON_TERMINAL_STATES, ...TERMINAL_STATES] as const;

export type IntentState = (typeof INTENT_STATES)[number];

/** One row of `GET /workspaces/{ws}/intents` — mirrors `workspaces._INTENT_COLUMNS` (Python). */
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

/**
 * The intent-keyed commands the web adapter offers — the four the queue
 * renders a button and a refusal sentence for. `cancel` takes the same
 * `{intent_id}` shape but is not offered here (decision 3 on #1033: an
 * overlay flag the worker honours, no audit row — its own follow-up). The
 * port re-validates the name, the role floor and the transition; this list
 * decides what the web tier fronts, nothing more.
 */
export const QUEUE_COMMANDS = ["approve", "mark_posted", "skip", "reject"] as const;

export type QueueCommand = (typeof QUEUE_COMMANDS)[number];

export const COMMAND_LABELS: Record<QueueCommand, string> = {
  approve: "Approve",
  mark_posted: "Posted myself",
  skip: "Skip",
  reject: "Reject",
};

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

/*
 * `idempotencyKeyFor` used to live here, keyed to `QueueCommand`. It moved to
 * `@/lib/commands` when the command client stopped being intent-only: the
 * derivation is the same expression for every command, and two copies of it —
 * one per dialect — is the shape this epic exists to remove. Not re-exported;
 * there is one caller and it imports from the new home.
 */

/**
 * A sentence per refusal, because the matrix's 409s are normal answers, not
 * errors: "this post already moved on" and "this workspace posts by hand" send
 * a person to different next moves, and neither is theirs to apologise for.
 * Keyed on the reason alone: every answer the queue sees comes through its
 * own route handler, which always names one.
 */
export function refusalCopy(reason: unknown): string {
  switch (reason) {
    case "illegal_transition":
      return "This post already moved on — the list has been refreshed.";
    case "manual_mode":
      return "This workspace posts by hand. Post the story on Instagram, then tap Posted myself.";
    case "not_found":
      return "This post is no longer in the queue.";
    case "unauthenticated":
      return "That session expired. Sign in again.";
    case "target_router_unreachable":
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

const SLOT_FORMAT: Intl.DateTimeFormatOptions = {
  weekday: "short",
  month: "short",
  day: "numeric",
  hour: "numeric",
  minute: "2-digit",
};

/**
 * One formatter per time zone, for the process. Constructing one costs ~50×
 * a format call, and the list formats every row on every render — a page of
 * 200 rows and a click is a few hundred constructions on the client alone.
 * A time zone Intl does not know (the column is CHECKed, so this is belt and
 * braces) gets the UTC formatter and is remembered as having fallen back.
 */
const slotFormatters = new Map<string, { format: Intl.DateTimeFormat; fellBack: boolean }>();

function slotFormatter(tz: string) {
  let entry = slotFormatters.get(tz);
  if (!entry) {
    try {
      entry = { format: new Intl.DateTimeFormat("en-US", { ...SLOT_FORMAT, timeZone: tz }), fellBack: false };
    } catch {
      entry = { format: new Intl.DateTimeFormat("en-US", { ...SLOT_FORMAT, timeZone: "UTC" }), fellBack: true };
    }
    slotFormatters.set(tz, entry);
  }
  return entry;
}

/**
 * The slot in the WORKSPACE's clock — a solo user reads their own time, never
 * UTC. Postgres renders fractional seconds at any width and `Date` only
 * promises three, so the fraction is trimmed first; an unknown time zone
 * renders in UTC and says so rather than throwing inside a list render.
 */
export function formatSlot(iso: string, tz: string): string {
  const date = new Date(iso.replace(/\.(\d{3})\d+/, ".$1"));
  const { format, fellBack } = slotFormatter(tz);
  const label = format.format(date);
  return fellBack ? `${label} UTC` : label;
}
