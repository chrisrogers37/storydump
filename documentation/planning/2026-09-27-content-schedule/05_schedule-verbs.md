---
title: "[plan] Phase 5: schedule and reschedule through the command port, and the CLI — content schedule"
type: plan
status: draft
owner: astrid
created: 2026-09-27
tags: [storydump, content-schedule, command-port, vocabulary, cli, audit, issue-1413]
repos: storydump
---

# Phase 5 — Schedule and reschedule through the port, and the CLI

## Summary

Part of #1413. Size: L. Two verbs create and move planned items (F11), and `cancel` ends them
(through phase 1). Each writes an audit event. The CLI gains the verbs and a way to list what is
coming. This is the first phase in which a planned row can exist, which is why it waits for
phases 1, 3 and 4.

## Evidence

- The vocabulary (`vocabulary.py:29-56`), the refusal reasons (`:58-72`, pinned total by the web
  adapter's status table) and the floors (`commands.py:105-111`; `approve` and `cancel` are
  `member`).
- `cancel` writes no audit row today, because the audit trigger fires only on a state change
  (`055:328-343`). An `INSERT`, and an update that changes no state, write none either.
- The audit writer: `audit.record` (`audit.py:42`).
- Account time zones: `ig_accounts.tz` falls back to `workspaces.tz`, which is how the clock
  computes slots (`083:96-100`).
- `svc_ingress` may `INSERT` and `UPDATE` `post_intents` (`057:100-106`).
- The CLI command-name gate: `tests/test_agent_docs.py` checks that every `storydump` verb named in
  `CLAUDE.md` and `AGENTS.md` exists.

## Implementation Plan

### Dependencies
Phases 1, 3 and 4; F3, F4, F7 and F11 locked.

### Blocks
Phase 6 (the web), phase 7.

### Steps
1. **Vocabulary.** Add `schedule_item` and `reschedule_item` to `COMMANDS` and `locked` to
   `REASONS`; both new verbs get floor `member`. Map `locked` in the web adapter's status table.
2. **`schedule_item`** `{ig_account_id, media_item_id, local_at, override_locks=false}`, in one
   transaction under the workspace's tenant:
   - Resolve the account in the workspace. It must be `active` or `reauth_required`; otherwise
     the verb refuses with `not_found`.
   - Convert `local_at` (a wall time with no offset) to an instant in SQL, using
     `COALESCE(a.tz, w.tz)`. A local time inside a DST gap is refused with `invalid_args`. An
     ambiguous one resolves to its first occurrence, and the result echoes the resolved instant.
   - Refuse, with `invalid_args`, an instant at or before now or more than 365 days ahead.
   - Apply the lock and item rule (F7): a blocker refuses with `locked`, stating the kind; a
     warning refuses with `locked` and `overridable: true`, unless `override_locks` is set, in
     which case the override goes into the audit detail.
   - `INSERT` with `origin = 'planned'`, `scheduled_by_user_id` set to the principal,
     `approval_mode = 'manual'`, `provider_account_ref` taken from the account, and
     `state = 'scheduled'`. A `uq_intent_live_subject` violation is mapped to
     `illegal_transition` ("already live for this account"). The database decides; there is no
     pre-check.
   - Write `audit.record` on the intent: `{kind: scheduled, at, tz, override?}`.
   - If the workspace has no active push binding, the result carries a warning that nothing can
     be served until a chat is bound. It is not a refusal.
3. **`reschedule_item`** `{intent_id, local_at}`: apply the same time rules, then run
   `UPDATE post_intents SET schedule_slot_at = :at WHERE id = :id AND origin = 'planned' AND
   state = 'scheduled' AND NOT cancel_requested`. If no row changes, the verb answers
   `illegal_transition` (already served, cancelled, or not planned). Audit `{kind: rescheduled,
   from, to}`.
4. **`cancel` audit.** It writes an explicit audit row on every intent, not only planned ones (G8;
   one line, and the gap exists for every origin).
5. **Reads.** The intents read gains `origin` and `scheduled_by` and an `origin` filter, so "what
   is coming" is the Queue read filtered to planned rows. There is no new endpoint.
6. **CLI.** Add verbs for schedule, reschedule and the planned list, named after the existing
   story verbs in `storydump_cli/`. Add them to the command lists in `CLAUDE.md` and `AGENTS.md`,
   and classify each for the NEVER-run list; that classification is a human judgement (F11's
   agent sub-question). Writes run only under a person-bound token with the operator role; a
   service token reads only (`PROJECT_MISSION.md`).
7. Add a `CHANGELOG.md` entry.

## Test Plan

- **`schedule_item`:** the happy path, and each refusal: a past time, beyond the horizon, a DST
  gap, removed media, a `reject` lock, a soft lock without the override (`locked`,
  `overridable`), a soft lock with it (created, and the override audited), a disabled account,
  and an item already live for the account (`illegal_transition`).
- **Floors:** a `member` passes; a read-only person token and a service token are refused.
- **`reschedule_item`:** the time moves in place; after the serve it answers `illegal_transition`.
- **`cancel`:** the row is flagged, phase 1's leg ends it `cancelled`, and it is never served.
- **Audit:** each of create, edit and cancel leaves a row naming the person.
- **Docs gate:** `tests/test_agent_docs.py` passes with the new verbs named.
- Revert check on each refusal.

## Verification Checklist

- [ ] The full suite passes, and `scripts/lint.sh` is clean.
- [ ] On a Neon branch, the CLI schedules an item for five minutes ahead; the row appears in the
      planned list, and at its time it is `awaiting_approval`.

## What NOT To Do

- Don't copy the workspace's `approval_mode` onto the row (the CHECK refuses anything but `manual`).
- Don't change an item or an account through `reschedule_item`. Changing those is a cancel plus a
  new schedule.
- Don't let the CLI or an agent approve through these verbs. Approval stays `approve`, done by a
  person.

## Context
- Source skill: forge · Area: `src/services/target/` (vocabulary, commands, command_executors, workspaces), `storydump_cli/` · Effort: L · Risk: Medium · Priority: High
