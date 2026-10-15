/**
 * The web adapter's command schema — one entry per command the dashboard offers.
 *
 * ## Why this exists ( /)
 *
 * One route fronts every command, and commands differ in body: an intent
 * command carries `intent_id`, a settings write carries a settings object and
 * no entity id. So **the intent-id check is one entry in this table rather
 * than a special case standing beside the dispatcher.** A per-command schema
 * with one hard-coded shape living outside it would be a second dialect.
 *
 * ## What a spec owns, and what it deliberately does not
 *
 * A spec parses the raw body and returns the payload to forward plus an
 * `identity` — the thing this submission is *about*, which the idempotency key
 * is derived from. That is all.
 *
 * It does **not** re-validate what the port validates. `settings_change` does not
 * carry a copy of the server's 13-key settings allowlist: a second copy of a
 * validation decision is one that can disagree with the first, and the one that
 * would go stale is this one. The spec checks *shape* (is there a settings object
 * at all); the port checks *keys and types* and refuses by name. Same reason the
 * BFF does not re-check membership.
 *
 * ## Identity
 *
 * For an intent command the identity is the intent id — a semantic key the server
 * can be handed twice with the same meaning. Two clicks produce one key, so the
 * second is a `200 replayed`.
 *
 * Entity-less commands have no such key.
 * Those are built with `submissionCommand`, whose identity is a client-supplied
 * `submission_id` — see its note for why per-ATTEMPT is the whole point and
 * per-click would be the bug.
 */

import { isUuid } from "./session";

/** The port refuses a key longer than this — the vocabulary's `IDEMPOTENCY_KEY_MAX` (`wire-contract.test.ts` pins them equal). */
export const IDEMPOTENCY_KEY_MAX = 200;

export type CommandParse =
  | { ok: true; body: Record<string, unknown>; identity: string }
  | { ok: false; error: string };

export type CommandSpec = {
  /** Parse the raw request body into what the port receives, plus an identity. */
  parse(raw: unknown): CommandParse;
};

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/**
 * An intent-keyed command: `{intent_id}`, and the intent is the identity.
 *
 * Nothing about it is privileged — it is a row like any other.
 */
function intentCommand(): CommandSpec {
  return {
    parse(raw) {
      if (!isPlainObject(raw)) return { ok: false, error: "malformed_body" };
      const intentId = raw.intent_id;
      if (!isUuid(intentId)) return { ok: false, error: "invalid_intent" };
      return { ok: true, body: { intent_id: intentId }, identity: intentId };
    },
  };
}

/**
 * A story's Cancel: keyed on the intent, like the CLI's
 * `cancel:<intent_id>`, so asking twice is one request. With `stop_repeat`
 * it also ends a repeating story's series, and is keyed apart, as
 * `cancel:<intent_id>:stop_repeat` (the CLI's key too): a plain cancel and
 * one that ends the series are two requests, and under one key the second
 * would be refused as a conflict rather than answered.
 */
function cancelCommand(): CommandSpec {
  return {
    parse(raw) {
      const parsed = intentCommand().parse(raw);
      if (!parsed.ok || !isPlainObject(raw) || raw.stop_repeat === undefined) {
        return parsed;
      }
      if (typeof raw.stop_repeat !== "boolean") {
        return { ok: false, error: "invalid_stop_repeat" };
      }
      return raw.stop_repeat
        ? {
            ok: true,
            body: { ...parsed.body, stop_repeat: true },
            identity: `${parsed.identity}:stop_repeat`,
          }
        : parsed;
    },
  };
}

/**
 * A `repeat` field as a spec admits it: absent, null (no rule, or the
 * clear), or an object riding as the person chose it, its shape and bounds
 * the port's to judge.
 */
function isRepeatField(value: unknown): boolean {
  return value === undefined || value === null || isPlainObject(value);
}

/**
 * A command with no entity to key on. The client supplies one `submission_id`
 * per submission ATTEMPT — reused across retries of that attempt, fresh for a
 * later deliberate one.
 *
 * Per-attempt is the whole point and per-click would be the bug: a fresh key on
 * every click is exactly the double execution `Idempotency-Key` exists to
 * prevent. The server backstops a mistake rather than trusting this: it stores a
 * SHA256 fingerprint of the payload beside the key, so a key reused with
 * DIFFERENT content is refused rather than silently applied (`machinery.py`:219-222).
 */
type BodyResult =
  { ok: true; body: Record<string, unknown> } | { ok: false; error: string };

function submissionCommand(
  parseBody: (raw: Record<string, unknown>) => BodyResult,
): CommandSpec {
  return {
    parse(raw) {
      if (!isPlainObject(raw)) return { ok: false, error: "malformed_body" };
      const submissionId = raw.submission_id;
      if (!isUuid(submissionId)) {
        return { ok: false, error: "invalid_submission_id" };
      }
      const parsed = parseBody(raw);
      if (!parsed.ok) return parsed;
      return { ok: true, body: parsed.body, identity: submissionId };
    },
  };
}

/** The port's three resolutions — the vocabulary's `RESOLUTIONS` (`wire-contract.test.ts` pins them equal). */
export const RESOLUTIONS = ["retry", "posted", "cancel"] as const;
/** The one verdict a resolution may carry — the vocabulary's `NOT_POSTED`. */
export const NOT_POSTED = "not_posted";

/**
 * The review card's one command: the intent and which of the
 * port's three resolutions. `episode` (the row's `entered_state_at`) is not
 * forwarded — the port has the row — but it is part of the identity, so a
 * double-click replays while a later review of the same post is a new key.
 */
function resolveReviewCommand(): CommandSpec {
  return {
    parse(raw) {
      if (!isPlainObject(raw)) return { ok: false, error: "malformed_body" };
      const intentId = raw.intent_id;
      if (!isUuid(intentId)) return { ok: false, error: "invalid_intent" };
      const resolution = raw.resolution;
      if (
        typeof resolution !== "string" ||
        !(RESOLUTIONS as readonly string[]).includes(resolution)
      ) {
        return { ok: false, error: "invalid_resolution" };
      }
      const episode =
        typeof raw.episode === "string" && raw.episode.length > 0 ? raw.episode : "";
      // The one verdict the port knows: the member looked, the story is not
      // there. Anything else is not forwarded — the port would refuse it.
      const verdict = raw.verdict === NOT_POSTED ? { verdict: NOT_POSTED } : {};
      return {
        ok: true,
        body: { intent_id: intentId, resolution, ...verdict },
        identity: episode
          ? `${intentId}:${resolution}:${episode}`
          : `${intentId}:${resolution}`,
      };
    },
  };
}

/**
 * The commands this tier offers. NOT the port's whole vocabulary — the port
 * re-validates the name, the role floor and the transition, and nothing here is
 * trusted downstream.
 *
 * Adding a row makes the route *capable* of a command. It does not wire a
 * control.
 */
export const COMMAND_SPECS: Record<string, CommandSpec> = {
  // Intent-keyed (the queue).
  approve: intentCommand(),
  mark_posted: intentCommand(),
  skip: intentCommand(),
  reject: intentCommand(),
  // A planned story's Cancel, and with `stop_repeat` the end of its
  // series.
  cancel: cancelCommand(),
  // The review card's resolutions.
  resolve_review: resolveReviewCommand(),

  // Entity-less: the identity is the submission, not a row. WIRED —
  // `command-client.ts`'s `submitSettingsChange` is the caller and
  // General's four switches and its schedule save are the controls.
  settings_change: submissionCommand((raw) => {
    // Shape only. The port owns which keys and types are legal.
    if (
      !isPlainObject(raw.settings) ||
      Object.keys(raw.settings).length === 0
    ) {
      return { ok: false, error: "invalid_settings" };
    }
    return { ok: true, body: { settings: raw.settings } };
  }),
  /**
   * The workspace's own name. The port's floor is `admin`, satisfied by an
   * owner.
   *
   * `/welcome` tells people "You can rename it later"; this row is
   * what keeps that promise.
   *
   * Shape only, as everywhere here: the port owns the length rule and does its
   * own trim. Blank is refused HERE because an empty name is the caller's
   * mistake and deserves a field-level answer rather than a round trip.
   */
  rename_workspace: submissionCommand((raw) => {
    if (typeof raw.name !== "string" || raw.name.trim().length === 0) {
      return { ok: false, error: "invalid_name" };
    }
    return { ok: true, body: { name: raw.name.trim() } };
  }),

  /**
   * The way OUT. Owner-only at the port (`ROLE_FLOOR`), and the port refuses a
   * body without `confirm: true` (`command_executors.py`): the destructive
   * intent must be STATED, not arrived at. The typed-name dialog is the front
   * end's half of an owner action that is explicit and confirmed; this is the
   * shape check that keeps an accidental empty POST from spending a round trip.
   *
   * The executor is at the port; this row offers it.
   * Irreversible once the 30-day grace window closes.
   */
  offboard_workspace: submissionCommand((raw) => {
    if (raw.confirm !== true) return { ok: false, error: "confirm_required" };
    return { ok: true, body: { confirm: true } };
  }),

  /**
   * The way back, inside the grace window. No body: the workspace is the URL
   * and the deadline is the port's to enforce, not this file's to
   * pre-judge.
   */
  restore_workspace: submissionCommand(() => ({ ok: true, body: {} })),
  // Pause Posting on Settings › General: one command per
  // direction; the port's admin floor and the flag's readers (the clock, the
  // prompt sweep, the publish leg) are all on the other side.
  pause_workspace: submissionCommand(() => ({ ok: true, body: {} })),
  resume_workspace: submissionCommand(() => ({ ok: true, body: {} })),

  /**
   * Disconnect the WORKSPACE's Google Drive (workspace Drive grant): the one grant is
   * revoked and every folder paused. No arguments — the grant is the
   * workspace's, so there is nothing to name.
   */
  disconnect_account: submissionCommand(() => ({ ok: true, body: {} })),

  /**
   * Remove a person from the workspace: an admin removes membership
   * explicitly — the revoke for every join edge, the Telegram one included.
   */
  remove_member: submissionCommand((raw) => {
    if (!isUuid(raw.user_id)) {
      return { ok: false, error: "invalid_user_id" };
    }
    return { ok: true, body: { user_id: raw.user_id } };
  }),

  /**
   * Invite a person by email. Only the address and the role are
   * forwarded: the web makes email invitations, so the Telegram arm's
   * `delivery_channel` and `invited_tg_user_id` never ride along. The port
   * owns what a valid address and an invitable role are; this checks only
   * that each is a string.
   */
  invite_member: submissionCommand((raw) => {
    if (typeof raw.email !== "string") {
      return { ok: false, error: "invalid_email" };
    }
    if (typeof raw.role !== "string") {
      return { ok: false, error: "invalid_role" };
    }
    return { ok: true, body: { email: raw.email, role: raw.role } };
  }),

  /**
   * Withdraw a pending invitation: an admin's Revoke. The link it
   * withdraws is refused at accept from then on; anything no longer pending
   * answers `not_found`.
   */
  revoke_invitation: submissionCommand((raw) => {
    if (!isUuid(raw.invitation_id)) {
      return { ok: false, error: "invalid_invitation_id" };
    }
    return { ok: true, body: { invitation_id: raw.invitation_id } };
  }),

  /**
   * Remove a destination: the port's
   * `active → disabled` edge. The row stays for history and for the connect
   * that brings the account back, so this is not a delete.
   */
  disable_account: submissionCommand((raw) => {
    if (!isUuid(raw.ig_account_id)) {
      return { ok: false, error: "invalid_account_id" };
    }
    return { ok: true, body: { ig_account_id: raw.ig_account_id } };
  }),

  /**
   * PER-SOURCE: the executor reads `source_id` and refuses `invalid_args`
   * without it (`command_executors.py:274`, via `_arg`), so an empty body can
   * never succeed.
   *
   * The id is validated here as a UUID for the same reason `intent_id` is: it
   * becomes a database lookup downstream, and a refusal shaped like "this is
   * not an id" is more useful than one shaped like "no such source".
   */
  sync_now: submissionCommand((raw) => {
    if (!isUuid(raw.source_id)) {
      return { ok: false, error: "invalid_source_id" };
    }
    return { ok: true, body: { source_id: raw.source_id } };
  }),

  /**
   * One account's schedule overrides. Capable, deliberately unwired: no
   * control sends it.
   *
   * Shape only, as everywhere here. The port owns which of the four columns
   * are legal and what type each takes, and it is the port that knows `null`
   * means INHERIT the workspace default rather than "unset" — a second copy of
   * that rule here is one that could disagree, and this is the copy that would
   * go stale.
   *
   * `settings` is checked non-empty for the same reason it is on
   * `settings_change`: an empty object is the caller's mistake and deserves a
   * field-level answer rather than a round trip. The id is checked as a UUID
   * for the reason `intent_id` and `source_id` are — it becomes a database
   * lookup, and "this is not an id" is a more useful refusal than "no such
   * account", which is also what the port must answer for someone else's.
   */
  account_settings_change: submissionCommand((raw) => {
    if (!isUuid(raw.ig_account_id)) {
      return { ok: false, error: "invalid_ig_account_id" };
    }
    if (
      !isPlainObject(raw.settings) ||
      Object.keys(raw.settings).length === 0
    ) {
      return { ok: false, error: "invalid_settings" };
    }
    return {
      ok: true,
      body: { ig_account_id: raw.ig_account_id, settings: raw.settings },
    };
  }),

  /**
   * Plan one item onto one account at a wall time:
   * the Media Library's Schedule…. `local_at` is the date and time a person
   * picked, with no offset; the port reads it in the ACCOUNT's zone, so it
   * rides as typed.
   *
   * Shape only, as everywhere here. The port owns the time rules (a past time,
   * one the clocks skip, one past the horizon) and the lock rule, and refuses
   * each by name; a second copy of either here is one that could disagree.
   * `override_locks` is forwarded only when it is `true`, which is how the CLI
   * sends it. A `repeat` rule is forwarded when it is an object.
   */
  schedule_item: submissionCommand((raw) => {
    if (!isUuid(raw.ig_account_id)) {
      return { ok: false, error: "invalid_ig_account_id" };
    }
    if (!isUuid(raw.media_item_id)) {
      return { ok: false, error: "invalid_media_item_id" };
    }
    if (typeof raw.local_at !== "string" || raw.local_at.trim() === "") {
      return { ok: false, error: "invalid_local_at" };
    }
    if (raw.override_locks !== undefined && typeof raw.override_locks !== "boolean") {
      return { ok: false, error: "invalid_override_locks" };
    }
    if (!isRepeatField(raw.repeat)) return { ok: false, error: "invalid_repeat" };
    return {
      ok: true,
      body: {
        ig_account_id: raw.ig_account_id,
        media_item_id: raw.media_item_id,
        local_at: raw.local_at,
        ...(raw.override_locks === true ? { override_locks: true } : {}),
        ...(isPlainObject(raw.repeat) ? { repeat: raw.repeat } : {}),
      },
    };
  }),

  /**
   * Move a planned story to another wall time: the Queue's
   * Reschedule…. The same row moves; its account cannot change. `local_at`
   * rides as typed, because the port reads it in the story's own zone.
   *
   * Keyed per submission, not on the intent: moving a story and then moving
   * it back is two acts, and an intent key would replay the first as the
   * second. The CLI mints a fresh key per run for the same reason.
   *
   * It also sets a story's repeat rule, or clears it with `null`: the
   * Queue's Edit series and Stop repeating. A time, a rule or both; `null` is
   * forwarded explicitly, because it is the clear, never a missing rule.
   */
  reschedule_item: submissionCommand((raw) => {
    if (!isUuid(raw.intent_id)) {
      return { ok: false, error: "invalid_intent" };
    }
    const timed = raw.local_at !== undefined;
    if (timed && (typeof raw.local_at !== "string" || raw.local_at.trim() === "")) {
      return { ok: false, error: "invalid_local_at" };
    }
    const ruled = raw.repeat !== undefined;
    if (!isRepeatField(raw.repeat)) return { ok: false, error: "invalid_repeat" };
    if (!timed && !ruled) return { ok: false, error: "nothing_to_change" };
    return {
      ok: true,
      body: {
        intent_id: raw.intent_id,
        ...(timed ? { local_at: raw.local_at } : {}),
        ...(ruled ? { repeat: raw.repeat } : {}),
      },
    };
  }),

  /**
   * Give an item the link its stories ask a person to add by hand,
   * or clear it with `null`: the Media Library's Link…. The link
   * belongs to the item, so every story of it shows the same one.
   *
   * Shape only, as everywhere here: a non-blank string, or `null` to clear.
   * The port owns the URL rule (https with a host, no spaces, no user name,
   * a length cap) and refuses a breach as `invalid_args`. `null` is forwarded
   * explicitly, because the port reads a missing key as a mistake, never as
   * a clear.
   *
   * Keyed per submission, like `reschedule_item`: setting a link, clearing it
   * and setting it again are three acts, and an item key would replay the
   * first as the third. The CLI mints a fresh key per run for the same reason.
   */
  set_item_link: submissionCommand((raw) => {
    if (!isUuid(raw.media_item_id)) {
      return { ok: false, error: "invalid_media_item_id" };
    }
    const link = raw.link_url;
    if (link !== null && (typeof link !== "string" || link.trim() === "")) {
      return { ok: false, error: "invalid_link_url" };
    }
    return { ok: true, body: { media_item_id: raw.media_item_id, link_url: link } };
  }),

  /**
   * One folder's own approval position, or `null` for Workspace default
   *: the Approvals card's folder select. An admin's, like
   * every position; the port holds the floor.
   *
   * Shape only, as everywhere here: a non-blank string, or `null`. The port
   * refuses a value outside the three positions by name, before it writes
   * (`set_source_approval`: `invalid_args`), and `ck_source_approval_mode`
   * (approval rules) bounds it again, so this keeps no copy of the three. `null` is
   * forwarded explicitly, as `set_item_link`'s clear is: a missing key is a
   * mistake, never Workspace default.
   *
   * Keyed per submission: YOLO, then Workspace default, then YOLO again are
   * three acts, and a folder key would replay the first as the third.
   */
  set_source_approval: submissionCommand((raw) => {
    if (!isUuid(raw.source_id)) {
      return { ok: false, error: "invalid_source_id" };
    }
    const mode = raw.approval_mode;
    if (mode !== null && (typeof mode !== "string" || mode.trim() === "")) {
      return { ok: false, error: "invalid_approval_mode" };
    }
    return { ok: true, body: { source_id: raw.source_id, approval_mode: mode } };
  }),

  /**
   * An item's Auto-approve flag, on or off: the Media Library's
   * toggle, and the Telegram card's Configure. Any member's, because the flag
   * is a standing Approve.
   *
   * The flag is a boolean and nothing else: a string "true" is the caller's
   * mistake, refused here by name. Keyed per submission, like the link: on,
   * off and on again are three acts.
   */
  set_item_auto_approve: submissionCommand((raw) => {
    if (!isUuid(raw.media_item_id)) {
      return { ok: false, error: "invalid_media_item_id" };
    }
    if (typeof raw.auto_approve !== "boolean") {
      return { ok: false, error: "invalid_auto_approve" };
    }
    return {
      ok: true,
      body: { media_item_id: raw.media_item_id, auto_approve: raw.auto_approve },
    };
  }),
};

export function isOfferedCommand(value: unknown): value is string {
  return typeof value === "string" && Object.hasOwn(COMMAND_SPECS, value);
}

export function parseCommand(command: string, raw: unknown): CommandParse {
  const spec = COMMAND_SPECS[command];
  if (!spec) return { ok: false, error: "unknown_command" };
  return spec.parse(raw);
}

/**
 * The ONE key derivation. Stable per (command, identity), so a retry of the same
 * submission replays and a different command on the same subject does not shadow
 * it — a refused Approve must not swallow a later Skip.
 */
export function idempotencyKeyFor(command: string, identity: string): string {
  return `${command}:${identity}`;
}
