/**
 * The web adapter's command schema — one entry per command the dashboard offers.
 *
 * ## Why this exists (#1057 / #1063, epic P2)
 *
 * The command route used to hard-code the intent-command shape: read `intent_id`,
 * check it is a UUID, forward `{intent_id}`. That worked because every command it
 * fronted was intent-keyed. It is also why the Settings surface could not use it —
 * a settings write has a different body and no entity id — so Settings kept
 * speaking a second, dead dialect through `postApi` instead.
 *
 * The fix is not more routes. It is that **the intent-id check becomes one entry
 * in this table rather than a special case standing beside the dispatcher.** A
 * per-command schema with one hard-coded shape still living outside it would be
 * the two-dialect defect surviving inside its own fix.
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
 * BFF deleted its membership re-check rather than porting it.
 *
 * ## Identity, and the one open question
 *
 * For an intent command the identity is the intent id — a semantic key the server
 * can be handed twice with the same meaning. Two clicks produce one key, so the
 * second is a `200 replayed`.
 *
 * Entity-less commands have no such key, which is the substance of the epic's F3.
 * Those are built with `submissionCommand`, whose identity is a client-supplied
 * `submission_id` — see its note for why per-ATTEMPT is the whole point and
 * per-click would be the bug.
 */

/** The port refuses a key longer than this (`v1.py:64`). */
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

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export function isUuidLike(v: unknown): v is string {
  return typeof v === "string" && UUID_RE.test(v);
}

/**
 * An intent-keyed command: `{intent_id}`, and the intent is the identity.
 *
 * This is the entry that used to be the dispatcher's hard-coded body handling.
 * Nothing about it is privileged now — it is a row like any other.
 */
function intentCommand(): CommandSpec {
  return {
    parse(raw) {
      if (!isPlainObject(raw)) return { ok: false, error: "malformed_body" };
      const intentId = raw.intent_id;
      if (!isUuidLike(intentId)) return { ok: false, error: "invalid_intent" };
      return { ok: true, body: { intent_id: intentId }, identity: intentId };
    },
  };
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
 * DIFFERENT content is refused rather than silently applied (`056`:219-222).
 */
type BodyResult =
  | { ok: true; body: Record<string, unknown> }
  | { ok: false; error: string };

function submissionCommand(
  parseBody: (raw: Record<string, unknown>) => BodyResult,
): CommandSpec {
  return {
    parse(raw) {
      if (!isPlainObject(raw)) return { ok: false, error: "malformed_body" };
      const submissionId = raw.submission_id;
      if (!isUuidLike(submissionId)) {
        return { ok: false, error: "invalid_submission_id" };
      }
      const parsed = parseBody(raw);
      if (!parsed.ok) return parsed;
      return { ok: true, body: parsed.body, identity: submissionId };
    },
  };
}

/**
 * The commands this tier offers. NOT the port's whole vocabulary — the port
 * re-validates the name, the role floor and the transition, and nothing here is
 * trusted downstream.
 *
 * Adding a row makes the route *capable* of a command. It does not wire a
 * control; that is the epic's P3/P4.
 */
export const COMMAND_SPECS: Record<string, CommandSpec> = {
  // Intent-keyed (the queue). Behaviour identical to what the route hard-coded.
  approve: intentCommand(),
  mark_posted: intentCommand(),
  skip: intentCommand(),
  reject: intentCommand(),

  // Entity-less. Capable, deliberately unwired until P3/P4.
  settings_change: submissionCommand((raw) => {
    // Shape only. The port owns which keys and types are legal.
    if (!isPlainObject(raw.settings) || Object.keys(raw.settings).length === 0) {
      return { ok: false, error: "invalid_settings" };
    }
    return { ok: true, body: { settings: raw.settings } };
  }),
  /**
   * PER-SOURCE, and the empty body this used to send could never succeed.
   *
   * The executor reads `source_id` and refuses `invalid_args` without it
   * (`command_executors.py:274`, via `_arg`), so `{}` made every call fail —
   * reachable, but not callable. P2 added the row deliberately unwired and
   * said so; only a real caller could find it, and P4 is that caller.
   *
   * The id is validated here as a UUID for the same reason `intent_id` is: it
   * becomes a database lookup downstream, and a refusal shaped like "this is
   * not an id" is more useful than one shaped like "no such source".
   */
  /**
   * The workspace's own name. Built and reachable at the port since the command
   * vocabulary existed (`admin` floor, satisfied by an owner) — and absent from
   * this allowlist, which is the only reason no one could rename anything.
   *
   * `/welcome` has been telling people "You can rename it later" the whole time
   * (#1152). The promise was correct; this is the wiring it was waiting on.
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
   * end's half of `06` §1's "owner (explicit, confirmed)"; this is the shape
   * check that keeps an accidental empty POST from spending a round trip.
   *
   * Built since the X.3 work (#1135) and unreachable because no adapter
   * offered it (#1127). Irreversible once the 30-day grace window closes.
   */
  offboard_workspace: submissionCommand((raw) => {
    if (raw.confirm !== true) return { ok: false, error: "confirm_required" };
    return { ok: true, body: { confirm: true } };
  }),

  /**
   * The way back, inside the grace window. No body: the workspace is the URL
   * and the deadline is the port's to enforce (#1185), not this file's to
   * pre-judge.
   */
  restore_workspace: submissionCommand(() => ({ ok: true, body: {} })),

  /**
   * Disconnect a Drive source. Same shape as `sync_now` and for the same
   * reason: the executor reads `source_id` and refuses `invalid_args` without
   * one, and the id becomes a database lookup, so "this is not an id" is a
   * more useful refusal than "no such source".
   *
   * Built since #1083 — command, executor, and the background Google revoke —
   * and unreachable because this table did not list it, while
   * `storydump.app/privacy` §13 committed to it publicly.
   */
  /**
   * Disconnect the WORKSPACE's Google Drive (069, #1165): the one grant is
   * revoked and every folder paused. No arguments — the grant is the
   * workspace's, so there is nothing to name.
   */
  disconnect_account: submissionCommand(() => ({ ok: true, body: {} })),

  /**
   * Remove a destination (owner decision 2026-09-04): the port's
   * `active → disabled` edge. The row stays for history and for the connect
   * that brings the account back, so this is not a delete.
   */
  /**
   * Remove a person from the workspace (`06`: "an admin removes membership
   * explicitly") — the revoke for every join edge, the Telegram one included.
   */
  remove_member: submissionCommand((raw) => {
    if (!isUuidLike(raw.user_id)) {
      return { ok: false, error: "invalid_user_id" };
    }
    return { ok: true, body: { user_id: raw.user_id } };
  }),

  disable_account: submissionCommand((raw) => {
    if (!isUuidLike(raw.ig_account_id)) {
      return { ok: false, error: "invalid_account_id" };
    }
    return { ok: true, body: { ig_account_id: raw.ig_account_id } };
  }),

  sync_now: submissionCommand((raw) => {
    if (!isUuidLike(raw.source_id)) {
      return { ok: false, error: "invalid_source_id" };
    }
    return { ok: true, body: { source_id: raw.source_id } };
  }),

  /**
   * One account's schedule overrides. Capable, deliberately unwired — same
   * posture `settings_change` was added under, and for the same reason: the
   * control is its own piece of work.
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
    if (!isUuidLike(raw.ig_account_id)) {
      return { ok: false, error: "invalid_ig_account_id" };
    }
    if (!isPlainObject(raw.settings) || Object.keys(raw.settings).length === 0) {
      return { ok: false, error: "invalid_settings" };
    }
    return {
      ok: true,
      body: { ig_account_id: raw.ig_account_id, settings: raw.settings },
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
