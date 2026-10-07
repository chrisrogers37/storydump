import { notAuthenticatedCopy } from "./refusal-copy";
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
 * WHY THESE COMMANDS AND NOT THE VOCABULARY. The matrix admits a human lever
 * from `awaiting_approval`: Approve (`approve`, and only where the workspace
 * can publish by API — otherwise the port refuses `manual_mode`), Posted
 * myself (`mark_posted`, the manual-mode path), Skip and Reject. A planned
 * story still in `scheduled` gets two more (#1413): Reschedule
 * (`reschedule_item`, which moves the same row) and Cancel (`cancel`, which
 * sets an overlay flag the worker honours, so the row reads Cancelling until
 * the reaper closes it). A cadence story in `scheduled` stays read-only: the
 * #1413 plan offers both levers on planned stories only. `autopost_now` is
 * unbuilt (501), a follow-up with its own semantics, not a missing entry here.
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

/**
 * `ck_intent_state`, the closed set — mirrors `workspaces.INTENT_STATES`
 * (Python); this comment is the grep handle from either side.
 *
 * THE ARRAYS ABOVE ARE THE ONLY SOURCE ON THIS TIER. `dashboard-payloads.ts`
 * used to declare a second partition as comma strings for the `?state=`
 * query; it now `join(",")`s these. There was never a reason for two, and
 * the two had already come apart: one of them had a row type missing
 * `account_handle`/`account_display_name`, which `_INTENT_COLUMNS` has
 * served since `06` §3.
 */
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
  /**
   * `planned`: a story a person scheduled for a chosen time (#1413).
   * `cadence`: the slot plan's own. Fixed when the row is born.
   */
  origin: "cadence" | "planned";
  /** Who planned it. NULL on a cadence row, and once that person is deleted. */
  scheduled_by_user_id: string | null;
  /** That person's display name, never an email. NULL exactly when the id is. */
  scheduled_by: string | null;
  /** The zone the story's time is read in: its account's, else the workspace's. */
  tz: string;
  /** Why the story missed its slot, when it did. */
  miss_reason: string | null;
  file_name: string;
  media_kind: string;
  thumbnail_url: string | null;
  caption: string | null;
  category: string | null;
  /** The item's link to add by hand, from its media row; shown only through `httpsHref`. */
  link_url: string | null;
  /** NULL when the account carries no handle — the key is always present. */
  account_handle: string | null;
  account_display_name: string | null;
};

export type IntentsResponse = { intents: Intent[]; limit: number };

/**
 * The Queue's Planned view: `?origin=planned`, which the page forwards to the
 * API's own `origin` filter. Filtering on the server keeps it exact past the
 * page limit, where a filter over the rows already loaded would miss some.
 */
export const QUEUE_HREF = "/dashboard/queue";
export const PLANNED_QUEUE_HREF = `${QUEUE_HREF}?origin=planned`;

/** The origin the Queue filters to, from its `origin` search param; anything else is every row. */
export function queueOriginFilter(param: unknown): "planned" | null {
  return param === "planned" ? "planned" : null;
}

/**
 * The intent-keyed commands the web adapter offers — the ones the queue
 * renders a button and a refusal sentence for, keyed on the intent so a
 * double-click replays. `cancel` joined them for planned stories (#1413; it
 * was decision 3 on #1033's follow-up). `reschedule_item` is not here: moving
 * a story twice is two acts, so it is keyed per submission (`@/lib/commands`).
 * The port re-validates the name, the role floor and the transition; this list
 * decides what the web tier fronts, nothing more.
 */
export const QUEUE_COMMANDS = [
  "approve",
  "mark_posted",
  "skip",
  "reject",
  "resolve_review",
  "cancel",
] as const;

export type QueueCommand = (typeof QUEUE_COMMANDS)[number];

/**
 * The buttons a row can carry: the approval card's four, the review card's
 * three (2026-09-12 — a `review_required` intent is the workspace's to
 * resolve), and a planned story's two (#1413). An action is a button; a
 * command is what the port runs — the three review actions are ONE command
 * with the resolution in the body, and Reschedule sends `reschedule_item`.
 */
export const QUEUE_ACTIONS = [
  "approve",
  "mark_posted",
  "skip",
  "reject",
  "retry",
  "resolve_posted",
  "resolve_cancel",
  "reschedule",
  "cancel",
] as const;

export type QueueAction = (typeof QUEUE_ACTIONS)[number];

/** The actions keyed on the intent; Reschedule is keyed per submission instead. */
export type IntentKeyedAction = Exclude<QueueAction, "reschedule">;

export const ACTION_LABELS: Record<QueueAction, string> = {
  approve: "Approve",
  mark_posted: "Posted myself",
  skip: "Skip",
  reject: "Reject",
  retry: "Post again",
  resolve_posted: "It posted",
  resolve_cancel: "Give up",
  reschedule: "Reschedule…",
  cancel: "Cancel",
};

type ReviewAction = "retry" | "resolve_posted" | "resolve_cancel";

const RESOLUTION_OF: Record<ReviewAction, string> = {
  retry: "retry",
  resolve_posted: "posted",
  resolve_cancel: "cancel",
};

function isReviewAction(action: QueueAction): action is ReviewAction {
  return action in RESOLUTION_OF;
}

export type ActionRequest = { command: QueueCommand; body: Record<string, unknown> };

/**
 * The command and body an action posts. A review resolution carries the
 * row's `entered_state_at` as its `episode`: the idempotency key is derived
 * from it (`@/lib/commands`), so a double-click replays while a LATER review
 * of the same post — parked again after a retry — is a new command. Post
 * again carries the member's verdict (`not_posted`): the Queue asks "is it
 * on your story?" before sending, and the port needs that answer when the
 * publish answer was lost — a plain retry could post the story twice.
 */
export function requestFor(
  action: IntentKeyedAction,
  intent: Pick<Intent, "id" | "entered_state_at">,
): ActionRequest {
  if (isReviewAction(action)) {
    return {
      command: "resolve_review",
      body: {
        intent_id: intent.id,
        resolution: RESOLUTION_OF[action],
        ...(action === "retry" ? { verdict: "not_posted" } : {}),
        episode: intent.entered_state_at,
      },
    };
  }
  return { command: action, body: { intent_id: intent.id } };
}

export function isQueueCommand(value: unknown): value is QueueCommand {
  return (
    typeof value === "string" && (QUEUE_COMMANDS as readonly string[]).includes(value)
  );
}

/**
 * The buttons a row gets. `awaiting_approval` has the matrix's four user
 * edges; hybrid (`api_publishing_enabled`) keeps the manual buttons beside
 * Approve (`06` §3). `review_required` has the three resolutions: It posted
 * only once a publish call was made (`publish_step`, on the row — before that
 * rung the port can only refuse it); Post again needs the API to publish;
 * Give up is offered even while a cancel is pending, because it IS the
 * cancel. A planned story still in `scheduled` can be rescheduled or
 * cancelled (#1413) — the port allows the move nowhere else. A story whose
 * item carries a link to add by hand gets no Approve: an app cannot attach a
 * link to a story it publishes, so such a story is posted by hand, as the
 * approval card hides Post now for it (fork F10 (a)). Every other state
 * renders read-only with its badge.
 */
export function actionsFor(
  state: IntentState,
  apiPublishingEnabled: boolean,
  cancelRequested = false,
  publishStep: string | null = null,
  origin: Intent["origin"] = "cadence",
  linked = false,
): QueueAction[] {
  if (state === "review_required") {
    if (cancelRequested) return ["resolve_cancel"];
    const actions: QueueAction[] = [];
    if (publishStep === "publish_called") actions.push("resolve_posted");
    if (apiPublishingEnabled) actions.push("retry");
    actions.push("resolve_cancel");
    return actions;
  }
  // A card whose cancellation is requested (by `cancel`, or because its
  // destination was removed) has no lever until the worker finishes it.
  if (cancelRequested) return [];
  if (state === "scheduled") {
    return origin === "planned" ? ["reschedule", "cancel"] : [];
  }
  if (state !== "awaiting_approval") return [];
  return apiPublishingEnabled && !linked
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
    case "not_connected":
      return "Instagram is not connected for this account. Connect it in Settings › Accounts, or post by hand.";
    case "nothing_to_confirm":
      return "Instagram did not post this one — it was never asked, or it answered no — so there is nothing to confirm. Post again, or give up.";
    case "may_have_posted":
      return "Instagram may have posted this one. Check your story first: choose It posted if it is there.";
    case "cancelling":
      return "This post is being cancelled.";
    case "not_found":
      return "This post is no longer in the queue.";
    case "unauthenticated":
      return notAuthenticatedCopy("Nothing changed.");
    // Two spellings, one cause. `target_router_unreachable` is what the
    // server-side `workspaceFetch` reports; `unreachable` is what `callBff`
    // reports when the browser's own `fetch` threw. `settingsRefusalCopy`
    // has paired them since it was written; this table now does too.
    case "unreachable":
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
