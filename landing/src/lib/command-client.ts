import { callBff, postJson } from "./bff";
import { refusalFacts, type RefusalFacts } from "./refusal-facts";
import { notAuthenticatedCopy, unreachableCopy } from "./refusal-copy";
/**
 * The browser's one door to the command route (#1057/#1063, epic P3).
 *
 * P2 made the route capable of an entity-less command; this is what a control
 * calls. It exists so the identity rule below is held in ONE place rather than
 * re-derived at each call site — the epic's whole subject is a surface that
 * grew two dialects, and four Settings controls each minting their own key
 * would be that defect arriving one layer further in.
 *
 * ## The identity rule, and why it is structural rather than careful
 *
 * `submission_id` is minted HERE, per call, and cannot be supplied by the
 * caller. That is deliberate: F3 locked a client-generated UUID per submit,
 * and a caller-supplied id is one a component can hold in `useState` and reuse
 * across every save for the life of a mount. The failure that would cause is
 * the one F3 rejected a content hash to avoid, and it is invisible:
 *
 *   enhanced -> simple   key K, content simple   → applied
 *   simple   -> enhanced key K, content enhanced → 409, the port refuses it
 *   enhanced -> simple   key K, content simple   → 200 `replayed`, NOT executed
 *
 * The third call reports success and writes nothing. Minting inside the
 * function makes that sequence unrepresentable rather than merely discouraged.
 *
 * A retry of the SAME attempt would want the same id, which this does not yet
 * express — no caller retries, and an unused parameter is the seam through
 * which a reused id arrives. Add it with the caller that needs it.
 *
 * ## `replayed` is a failure here, not a success
 *
 * The port answers a same-key/same-body call with HTTP 200 and
 * `{"outcome":"replayed"}` — acknowledged, deliberately NOT executed
 * (`app.py:247-250`). For an intent command that is the correct, harmless
 * answer: the row already moved. For a settings write it is the F3 harm
 * exactly — "the user sees a success and the setting does not move, and the UI
 * takes the blame."
 *
 * With a fresh id per call it is unreachable. So it is reported as a failure
 * rather than smoothed over: if it ever arrives, the identity rule above has
 * broken, and the one thing the user must not be told is that their change
 * was saved.
 */

export type SubmitResult =
  | { ok: true; data: Record<string, unknown> }
  | { ok: false; error: string; status: number; facts?: RefusalFacts };

/**
 * The reason string for a `replayed` answer. Its own code, not folded into a
 * generic failure: it means the write did not happen AND the key mechanism is
 * wrong, which is a different remedy from a refusal by the port.
 */
export const REPLAYED_ERROR = "unexpected_replay";

/** The one path spelling for a command. The queue imports this rather than re-typing it. */
export function commandPath(workspaceId: string, command: string): string {
  return `/api/workspaces/${workspaceId}/commands/${command}`;
}

/**
 * POST one command for a workspace, with a fresh submission identity.
 *
 * Returns a typed result rather than throwing. A thrown error would arrive at
 * a component's `catch` beside genuine network failures, and "the port refused
 * this value" and "the browser could not reach the app" want different
 * sentences.
 */
export async function submitCommand(
  workspaceId: string,
  command: string,
  body: Record<string, unknown> = {},
): Promise<SubmitResult> {
  const result = await callBff(
    commandPath(workspaceId, command),
    // The id rides in the body because that is where P2's `submissionCommand`
    // spec reads it; the route derives the header from it and the browser
    // never sets `Idempotency-Key` itself.
    postJson({ ...body, submission_id: crypto.randomUUID() }),
  );

  if (!result.ok) {
    // THE ONE CALLER THAT READS TWO KEYS, spelled out rather than pushed
    // into `callBff`. The port's 4xx carry `{"error": …}` and its 409s carry
    // `{"reason": …}`; `callBff` reads the first and falls back to
    // `http_<status>`. Re-deriving here keeps the precedence identical to
    // what shipped (`error`, then `reason`, then whatever `callBff` already
    // decided) instead of inferring it from the fallback string, which would
    // misread a route that legitimately answered `{"error": "http_409"}`.
    //
    // The last arm is `result.error`, NOT a re-synthesised `http_${status}`:
    // a thrown `fetch` has no body at all and `callBff` answers it
    // `unreachable` at status 0, which `settingsRefusalCopy` has a sentence
    // for. Rebuilding the string here would spell that case `http_0` and
    // drop it through to the generic refusal.
    const error =
      typeof result.body.error === "string"
        ? result.body.error
        : typeof result.body.reason === "string"
          ? result.body.reason
          : result.error;
    // Re-checked with the route's own allow-list, so the browser holds nothing
    // the list would not pass, whatever arrived.
    const facts = refusalFacts(result.body.facts);
    return facts
      ? { ok: false, error, status: result.status, facts }
      : { ok: false, error, status: result.status };
  }

  if (result.data.outcome === "replayed") {
    return { ok: false, error: REPLAYED_ERROR, status: result.status };
  }

  return { ok: true, data: result.data };
}

/**
 * Submit a `settings_change`. The port owns which keys are legal and refuses
 * an unknown one BY NAME (`workspaces.py:466`), so nothing here re-states the
 * allowlist — a second copy is one that can disagree, and this is the copy
 * that would go stale.
 */
export function submitSettingsChange(
  workspaceId: string,
  settings: Record<string, unknown>,
): Promise<SubmitResult> {
  return submitCommand(workspaceId, "settings_change", { settings });
}

/** Rename the workspace. `submitCommand` mints the submission id. */
export function submitRenameWorkspace(workspaceId: string, name: string) {
  return submitCommand(workspaceId, "rename_workspace", { name });
}

/**
 * Start offboarding (#1127). The intent rides in the body because the port
 * requires it stated; the dialog that gathered it is the caller's.
 */
export function submitOffboardWorkspace(workspaceId: string) {
  return submitCommand(workspaceId, "offboard_workspace", { confirm: true });
}

/** Restore a workspace inside its grace window (#1185). */
export function submitRestoreWorkspace(workspaceId: string) {
  return submitCommand(workspaceId, "restore_workspace");
}

/**
 * Remove a destination (owner decision 2026-09-04): `disable_account`. The
 * schedule stops, the Instagram credential is revoked, posts waiting for
 * approval are cancelled; connecting the same account again brings it back.
 */
export function submitDisableAccount(workspaceId: string, accountId: string) {
  return submitCommand(workspaceId, "disable_account", { ig_account_id: accountId });
}

export function disableAccountRefusalCopy(reason: unknown, status?: number): string {
  if (status === 403 || reason === "insufficient_role") {
    return "You need to be an admin of this workspace to remove a destination.";
  }
  switch (reason) {
    case "not_found":
      return "That destination is no longer here. Reload the page.";
    case "illegal_transition":
      return "That destination was already removed. Reload the page.";
    case REPLAYED_ERROR:
      return "That did not go through — the app sent it under a key the server had already seen. Reload and try again; report this if it repeats.";
    case "unauthenticated":
    case "http_401":
      return notAuthenticatedCopy("Nothing changed.");
  }
  return "Could not remove that destination. Nothing changed — try again shortly.";
}

/** The warning a planned story's answer carries when no chat is bound (the vocabulary's `NO_PUSH_BINDING`). */
export const NO_PUSH_BINDING = "no_push_binding";

/** How far ahead a story may be planned, in days (the vocabulary's `PLAN_HORIZON_DAYS`). */
export const PLAN_HORIZON_DAYS = 365;

export type SchedulePlan = {
  accountId: string;
  itemId: string;
  /** The date and time a person picked, as typed: no offset; the port reads it in the account's zone. */
  localAt: string;
  /** Only when the person chose to override a lock the port said can be overridden. */
  overrideLocks?: boolean;
};

/**
 * Plan one item onto one account at a wall time (#1413 phase 6). The time
 * rides as typed, because the port reads it in the account's zone, never the
 * browser's.
 */
export function submitScheduleItem(workspaceId: string, plan: SchedulePlan) {
  return submitCommand(workspaceId, "schedule_item", {
    ig_account_id: plan.accountId,
    media_item_id: plan.itemId,
    local_at: plan.localAt,
    ...(plan.overrideLocks ? { override_locks: true } : {}),
  });
}

/**
 * Each lock kind as the clause that finishes a refusal, keyed by the
 * vocabulary's `BLOCKING_LOCKS` and `WARNING_LOCKS` (`wire-contract.test.ts`
 * holds the keys equal). `skip` and `reject` are a person's answers to an
 * approval card and `recent` is the repost lock an account gets when the item
 * posts there. Nothing writes the other three today, so they say only what the
 * item is marked.
 */
export const LOCK_CLAUSES: Record<string, string> = {
  skip: "it was skipped recently",
  recent: "it was posted on this account recently",
  reject: "it was rejected",
  unsupported: "it is marked unsupported",
  hold: "it is on hold",
  seasonal: "it is marked seasonal",
};

/** `item_<state>`: the item itself is what is in the way. */
const ITEM_GONE_CLAUSE = "it is no longer available";

/** Except an item Instagram cannot post, which is still in the library: the remedy is another item. */
const ITEM_UNSUPPORTED_CLAUSE = "Instagram can't post it";

/**
 * A refused planned time, by the rule it broke (`facts.at_rule`), keyed by the
 * vocabulary's `AT_RULE_SENTENCES` (`wire-contract.test.ts` holds the keys
 * equal, so a rule the port adds cannot fall through to the sentence with none).
 */
export const AT_RULE_COPY: Record<string, string> = {
  past: "That time has already passed on the account's clock. Pick a later one.",
  skipped:
    "That time does not happen on the account's clock: a daylight-saving change skips it. Pick another.",
  horizon: `That is more than ${PLAN_HORIZON_DAYS} days ahead. Pick a sooner time.`,
  shape: "Enter a date and a time.",
  not_a_date: "Enter a date and a time.",
};

function lockClauses(inTheWay: readonly string[] | undefined): string[] {
  const clauses: string[] = [];
  for (const kind of inTheWay ?? []) {
    if (kind === "item_unsupported") clauses.push(ITEM_UNSUPPORTED_CLAUSE);
    else if (kind.startsWith("item_")) clauses.push(ITEM_GONE_CLAUSE);
    else if (Object.hasOwn(LOCK_CLAUSES, kind)) clauses.push(LOCK_CLAUSES[kind]);
  }
  return clauses;
}

function capitalized(sentence: string): string {
  return sentence.charAt(0).toUpperCase() + sentence.slice(1);
}

/** The question before an override: what it gets past, then whether to go ahead. */
export function scheduleOverrideCopy(facts: RefusalFacts | undefined): string {
  const clauses = lockClauses(facts?.in_the_way);
  return clauses.length > 0
    ? `${capitalized(clauses.join(" and "))}. Schedule it anyway?`
    : "This item has a lock on it. Schedule it anyway?";
}

/**
 * A sentence for a `schedule_item` refusal. With the refusal's facts each one
 * names its remedy; without them each still says what kind of refusal it was.
 */
export function scheduleRefusalCopy(
  reason: unknown,
  status?: number,
  facts?: RefusalFacts,
): string {
  if (status === 403 || reason === "insufficient_role") {
    return "You need to be a member of this workspace to schedule a story.";
  }
  switch (reason) {
    case "locked": {
      const clauses = lockClauses(facts?.in_the_way);
      return clauses.length > 0
        ? `This item can't be scheduled: ${clauses.join(" and ")}.`
        : "A lock, or the item itself, keeps it from being scheduled.";
    }
    case "illegal_transition": {
      // A duplicate. Its `existing` may be absent when that story ended before
      // it could be read, and absent never means there was no conflict.
      const existing = facts?.existing;
      if (existing?.cancel_requested) {
        return "This item's story on that account was cancelled a moment ago. Plan it again once it has cleared.";
      }
      if (existing?.origin === "planned") return "This item is already scheduled on that account.";
      if (existing?.origin === "cadence") {
        return "This item is already waiting in that account's regular slots.";
      }
      return "This item is already waiting to post on that account.";
    }
    case "invalid_args": {
      const rule = facts?.at_rule;
      return rule && Object.hasOwn(AT_RULE_COPY, rule)
        ? AT_RULE_COPY[rule]
        : `That time can't be used. Pick one later than now and within ${PLAN_HORIZON_DAYS} days.`;
    }
    case "not_found":
      if (facts?.missing === "account") return "That account is no longer here. Reload the page.";
      if (facts?.missing === "item") return "That item is no longer in the library. Reload the page.";
      return "That account or item is no longer here. Reload the page.";
    case REPLAYED_ERROR:
      return "That did not go through — the app sent it under a key the server had already seen. Reload and try again; report this if it repeats.";
    case "unauthenticated":
    case "http_401":
      return notAuthenticatedCopy("Nothing changed.");
    case "unreachable":
    case "target_router_unreachable":
      return unreachableCopy("Nothing was scheduled");
  }
  return "Could not schedule that. Nothing was scheduled — try again shortly.";
}

/**
 * A sentence for an offboarding or restore refusal. Its own vocabulary, like
 * `settingsRefusalCopy`: these reasons cannot arise from a settings write, and
 * the 403 here means something narrower — only the OWNER may do this, so the
 * settings sentence ("an admin or the owner can") would be wrong.
 */
export function offboardingRefusalCopy(reason: unknown, status?: number): string {
  if (status === 403) {
    return "Only the workspace owner can delete or restore it.";
  }
  switch (reason) {
    case "confirm_required":
      return "Type the workspace name exactly to confirm.";
    case "illegal_transition":
      // Three port refusals share this reason: already deleting, not deleting,
      // and the restore window having closed. All three mean the screen is
      // behind the workspace, and the remedy is the same.
      return "This workspace is not in a state that allows that — it may already be deleting, or the restore window may have closed. Reload to see its current state.";
    case "not_found":
      return "This workspace no longer exists.";
    case REPLAYED_ERROR:
      return "That did not go through — the app sent it under a key the server had already seen. Reload and try again; report it if it repeats.";
    case "unauthenticated":
      return notAuthenticatedCopy("Nothing changed.");
    case "unreachable":
    case "target_router_unreachable":
      return unreachableCopy("Nothing changed");
  }
  return "That did not go through. Nothing changed — try again shortly.";
}

/**
 * A sentence for a settings refusal.
 *
 * Deliberately NOT `intents.ts`'s `refusalCopy`, which is not a shared table
 * that happens to be elsewhere — it is the QUEUE's vocabulary. Its reasons
 * (`illegal_transition`, `manual_mode`) cannot arise from a settings write and
 * these cannot arise from a queue action, so folding them together would put
 * "This post is no longer in the queue" on a form about posting hours. Two
 * disjoint vocabularies, not two copies of one decision.
 */
export function settingsRefusalCopy(reason: unknown, status?: number): string {
  // A permission refusal carries NO reason to switch on. `insufficient_role`
  // is mapped to 403 and answered `{detail: "forbidden"}` — no `error`, no
  // `reason` — so `submitCommand` synthesises `http_403` and every branch below
  // misses. Without this, a member refused for their ROLE and a browser that
  // could not reach the app got the same sentence, and the remedy for one is
  // "ask an admin" while the remedy for the other is "try again".
  //
  // Keyed on the STATUS rather than the synthesised string, because the status
  // is the real signal and `http_403` is only a stand-in for it. On this path
  // 403 has exactly one cause: a non-member is answered 404, never 403
  // (`v1.py:9`, deliberate non-disclosure), so a 403 means the caller IS a
  // member and their role is too low.
  if (status === 403) {
    return "You do not have permission to change this. An admin or the workspace owner can.";
  }

  switch (reason) {
    case REPLAYED_ERROR:
      // Not smoothed into the generic line: this one means the change did NOT
      // land, which the generic line also says, AND that the key mechanism is
      // broken, which someone needs to see.
      return "That change was not saved — the app sent it under a key the server had already seen. Reload and try again; report it if it repeats.";
    case "invalid_settings":
      return "Nothing to save — no setting changed.";
    case "invalid_name":
      return "Give the workspace a name.";
    case "unauthenticated":
      return notAuthenticatedCopy("Nothing changed.");
    case "unreachable":
    case "target_router_unreachable":
      return unreachableCopy("Nothing changed");
  }
  return "That did not save. Nothing changed — try again shortly.";
}

/**
 * Remove a person from the workspace (`06`: "an admin removes membership
 * explicitly"). The owner cannot be removed here and nobody removes
 * themselves; the API says which.
 */
export function submitRemoveMember(workspaceId: string, userId: string) {
  return submitCommand(workspaceId, "remove_member", { user_id: userId });
}

export function removeMemberRefusalCopy(reason: unknown, status?: number): string {
  if (status === 403 || reason === "insufficient_role") {
    return "You need to be an admin of this workspace to remove a member.";
  }
  switch (reason) {
    case "not_found":
      return "That person is no longer a member. Reload the page.";
    case "illegal_transition":
      return "The owner cannot be removed, and you cannot remove yourself. Nothing changed.";
    case REPLAYED_ERROR:
      return "That did not go through — the app sent it under a key the server had already seen. Reload and try again; report this if it repeats.";
    case "unauthenticated":
    case "http_401":
      return notAuthenticatedCopy("Nothing changed.");
  }
  return "Could not remove that member. Nothing changed — try again shortly.";
}

/**
 * Invite a person by email (#1563). The answer is the only place the
 * invitation's join link exists in full — the port keeps a hash of its
 * token — so the caller shows it once and keeps it nowhere else.
 */
export function submitInviteMember(
  workspaceId: string,
  invite: { email: string; role: string },
): Promise<SubmitResult> {
  return submitCommand(workspaceId, "invite_member", {
    email: invite.email,
    role: invite.role,
  });
}

export function inviteMemberRefusalCopy(reason: unknown, status?: number): string {
  if (status === 403 || reason === "insufficient_role") {
    return "You need to be an admin of this workspace to invite someone.";
  }
  switch (reason) {
    // The route refuses a non-string address; the port gives every invitation
    // refusal `invalid_args`, and the form's role select cannot send a bad role.
    case "invalid_email":
    case "invalid_args":
      return "Check the email address and try again. Nothing was created.";
    case "invalid_role":
      return "Choose Member or Admin. Nothing was created.";
    case REPLAYED_ERROR:
      return "That invitation was already made, and its link cannot be shown again. Invite them again for a new link.";
    case "unauthenticated":
    case "http_401":
      return notAuthenticatedCopy("Nothing was created.");
    case "unreachable":
    case "target_router_unreachable":
      return unreachableCopy("Nothing was created");
  }
  return "That invitation did not go through. Nothing was created — try again shortly.";
}
