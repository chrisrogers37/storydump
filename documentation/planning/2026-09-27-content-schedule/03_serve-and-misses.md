---
title: "[plan] Phase 3: serve planned items on time, never miss in silence — content schedule"
type: plan
status: draft
owner: astrid
created: 2026-09-27
tags: [storydump, content-schedule, prompts, reaper, notices, migration, issue-1413]
repos: storydump
---

# Phase 3 — Serve on time, and never miss in silence

## Summary

Part of #1413. Size: L. Planned rows are served by the prompt sweep only while they can be served
(F7) and are still within the late window (F9). Every other ending of an unserved planned row goes
through a new miss door, which expires the row with its reason and tells the bound chats in the
same transaction. The reaper's silent expiry leg stops touching planned rows, and no row is served
while cancel-flagged. The approval card says who scheduled the item. This phase also contracts
the slot index that phase 2 expanded.

## Evidence

- The serve door is `fn_prompts_due`, `082:105-121`. It filters on `state = 'scheduled'`, time,
  and an active, unpaused workspace, and nothing else (overview, G2 and G3).
- The reaper's silent leg is `076:33-36`, with no grace (G1). Phase 1 lands first, so the current body
  is phase 1's migration.
- The notice path to copy is `_notice_no_media` (`scheduler.py:206-292`): outbox rows per active
  push binding, deduplicated, with an undeliverable verdict when no surface exists.
- Account removal already flags live intents (`provisioning.py:834`).
- There is an existing card label for the state: `"scheduled": "🗓 Scheduled"` (`prompts.py:69`).

## Implementation Plan

### Dependencies
Phase 1 merged: both phases redefine `fn_reaper_sweep`, and this one starts from phase 1's body.
Phase 2 deployed and drained on every worker: the index contract needs it, and `railway.toml:30`
sets `drainingSeconds = 60`, so old and new workers overlap for up to a minute on each deploy.
The owner's DDL approval; F5, F7 and F9 locked.

### Blocks
Phase 5: planned rows must be honest before anything can create them. Phase 7 builds on the card.

### Steps
1. **A new migration**, taking the next free number:
   - **Contract the slot key:** `DROP INDEX uq_intent_slot;` then
     `ALTER INDEX uq_intent_slot_cadence RENAME TO uq_intent_slot;`. Conflict inference matches
     on columns and predicate, not on the name, and the rename keeps every doc reference valid.
     State "phase 2 is deployed and drained on every worker" as a merge precondition in the PR.
   - **`fn_prompts_due`:** drop and re-create it, because its row type changes. Keep the comment,
     the owner and the grants that follow it (`082:123-130`). The new signature is
     `fn_prompts_due(p_limit int, p_late interval)`, and it adds:
     - `AND NOT i.cancel_requested`, for every origin;
     - for planned rows, the servable predicate from F7 (media `available`, account `active` or
       `reauth_required`, no blocking lock) and `i.schedule_slot_at > now() - p_late`;
     - two return columns, `o_origin` and `o_scheduled_by` (the scheduler's display name).
   - **`fn_reaper_sweep`:** its first expiry leg gains `AND origin = 'cadence'`. Start from the
     current live definition, which is phase 1's migration and includes its cancel leg.
   - **New door `fn_planned_misses(p_limit int, p_late interval)`:** SECURITY DEFINER, owned by
     `svc_maintenance`, EXECUTE for `svc_worker`, in the `082` door pattern. It returns due,
     planned, `scheduled`, unflagged rows that are unservable, or past `now() - p_late`, or (if
     F9 is (ii)) in a paused workspace, each with one reason in this precedence: `item_removed`,
     `item_unsupported`, `item_locked`, `account_removed`, `paused`, `late`.
2. **The miss leg** (in `prompts.py`, beside `sweep_due_prompts`, on the prompt sweep's cadence):
   for each workspace, under its tenant, in one transaction, run the guarded
   `UPDATE post_intents SET state = 'expired', last_error = '{"v":1,"class":"planned_missed","message":<reason>}'
   WHERE id = :id AND state = 'scheduled'`, then queue one notice per active push binding:
   "🗓 Not served: <file> for @<handle>, scheduled for <local time> by <name>: <reason>. Nothing
   was posted." Carry the undeliverable verdict the way `_notice_no_media` does, and add counters
   to the status line the way the prompt sweep has them.
3. **The card:** a planned card gains the line "🗓 Scheduled by <name> · <local time>", plus
   "served late" when it was served after its time.
4. **Account removal:** when `disable_account` or `move_account` flags planned rows, queue the
   same notice with reason `account_removed` in that transaction.
5. **Worker configuration:** add `planned_late_seconds` (F9's value), passed to both doors.
6. Update the advertised DDL (`07` for the doors, `02` for the index) in step, and add a
   `CHANGELOG.md` entry.

## Test Plan

- **On time:** a due planned row is served; it reaches `awaiting_approval`, and its card carries
  the scheduled line.
- **Cancelled:** a flagged planned row is never served and never gets a miss notice; phase 1's
  leg ends it `cancelled`.
- **Each miss reason:** removed media, `reject` lock, disabled account, paused past the window,
  and a sweep that did not run until after the window. Each ends `expired` with its `last_error`
  and a queued notice, and none is left `scheduled` or expired silently.
- **Late but in time:** a pause that ends inside the window serves the row late, marked as late.
- **Cadence unchanged:** the existing `plan_slot`, prompt and reaper tests pass, and a past-due
  cadence row still expires through the reaper.
- **Every reaper leg survives:** the phase 1 leg set, re-run against the new body.
- **The slot key after contract:** a duplicate cadence mint still yields one intent, and a
  planned row may share the instant.
- Revert check on every guard.

## Verification Checklist

- [ ] The full suite passes, including the replay gate.
- [ ] On a Neon branch, with the worker stopped across a planned row's time plus the window and
      then started: the row is `expired` with `last_error->>'class' = 'planned_missed'`, and the
      outbox holds its notice.
- [ ] `SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_intent_slot'` shows
      `WHERE (origin = 'cadence'::text)`.

## What NOT To Do

- Don't ship the contract step before phase 2 is deployed and drained on every worker (overview, G6).
- Don't send a miss notice for a cancelled row. The person already knows.
- Don't put the servability rule in Python beside the door (F5).
- Don't let the miss leg write `cancelled`. Cancellation is phase 1's leg alone.

## Build notes (2026-09-29): built as 089 on #1449's head

**Where it lives:** PR #1471, branch `astrid/1413-p3-serve-and-misses-089`. It was built on #1449's
head `3ee9053a` (phase 2 as **088 / `07` §31 / ordinal 29**) while the owner's rule held that no
storydump PR stacks on another; #1449 has since squash-merged to main (`ae6bbb4d`) and this phase
opened as a PR with main merged in. This phase is **089 / `07` §32 / manifest ordinal 30**. The
earlier branch `astrid/1413-p3-serve-and-misses` (the retired stack, numbered 087) is superseded;
its four commits were carried forward, renumbered line by line.

**Merge and deploy precondition (G6), for the PR body:** phase 3 ships only after **088 is deployed
and drained on every worker**. 089 drops the unconditional slot key, and a worker still running
pre-088 code spells `plan_slot`'s conflict target without the predicate: against the partial key
alone it finds no arbiter, so every cadence mint raises (`railway.toml`'s `drainingSeconds = 60` is
the overlap). #1449 and this phase must never go out in one deploy. Plus the owner's DDL approval.
The number 089 is provisional.

### Where the build departs from the steps above

- **`fn_prompts_due(p_limit int, p_late interval DEFAULT NULL)`.** Dropping the one-argument door
  would make the draining worker's call raise for the whole deploy overlap: G6's hazard, applied
  to a door. With the default, the old call still resolves, and a NULL window serves no planned
  row. `fn_planned_misses` takes no default and is `STRICT`: a missing number never expires a row.
- **The door returns the scheduler's user id** (`o_scheduled_by_user_id`), not a display name. The
  doors' owner cannot read `user_identities`, and the name rule (a chat sees a display name, never
  an email) stays spelled once, in `identity.display_name_for`.
- **The lock reads are the workspace's.** `ck_locks_recent_scope` makes `reject`, `unsupported`,
  `hold` and `seasonal` workspace-wide, and both doors say so (`l.ig_account_id IS NULL`), which is
  what lets them use `uq_lock_ws_scope`. Measured on a production copy: without it, the lock read is
  a sequential scan of every live lock, on every beat, even with sequential scans switched off.
- **`paused` means the workspace was not taking posts** (paused, or in a state other than
  `active`), judged when the window closes. That is the complement of the serve door's workspace
  predicate, so the two planned doors partition the due rows exactly.
- **Only `disable_account` removes a destination** (nothing in `src/` writes `moved`). Step 4 is
  `prompts.say_removed_before_served`, called by `provisioning.disable_destination` with the ids its
  flag write returned: only the planned stories THIS removal flags before their time are told. It
  reads `prompts._NOTICE_SELECT`, which is pinned to the miss door's body as `_CARD_SELECT` is to
  the due door's.
- **The card keeps its Slot line** and gains the scheduled line; "served late" is judged when the
  card is rendered, a minute past its time (`prompts.SERVED_LATE_AFTER`, pinned above the worker's
  own beats).
- **`fn_reaper_sweep` is 087's live body byte for byte** (the lease, cancel, expiry, lock, invitation
  and onboarding legs, then #1452's deadline leg last) plus `AND origin = 'cadence'` on the slot
  expiry and its comment. It still carries the text 086's and 087's adoption probes read.
- **All the DDL is in `07`**, the contract included. `02` §3's SQL is 055's stream and immutable, so
  `02` gains a prose note beside key 1 and in the reaper's remit.
- **Phase 2 had to change once** (#1449, `d8fb8acf`): its adoption probe matches the slot key by its
  definition, because this phase renames the key and `runner adopt` refuses a false probe below a
  true one. 089's own probes name the doors by name and owner, not arity, for the same reason.
- **Three gates learned something.** The tenancy gate *handles* `ALTER INDEX` rather than allowlisting
  it: PostgreSQL also renames a TABLE through `ALTER INDEX … RENAME` (measured on 15), and a table's
  name is what its facts are keyed by, so that spelling is refused. The lineage lane now asks every
  target file's probes again at the head. And #1417's battery (`content_schedule_01b.sh`) edits the
  final `fn_reaper_sweep` body, whichever section holds it: once 089 re-defines the function, its
  edits to §30 would have changed nothing, and every leg mutant would have survived.

### Evidence (on `df81843b`)

- **The full suite**, locally under the host's lock: 4092 passed, 1 failed, 1 skipped (the scheduled
  live-drift audit), 5 deselected (the load harness), in 45 minutes. The failure is this host's, not
  the branch's: `test_ops_views_gate.py::test_the_predicates_confine_rows_even_without_row_level_security`
  needs a test role with BYPASSRLS, which this host's test cluster lacks (CI's is a superuser). CI has
  not run: the workflows run on a PR, and there is none yet.
- **The mutation batteries**, each in its own worktree at the head, each tree clean afterwards:
  - `content_schedule_03.sh` (this phase): **41 of 41 killed**. That includes both doors' lock expiry,
    `w.state`, and window edge, and the miss door's own flag filter, which a gate-level test could not
    see until the test read the door directly.
  - `content_schedule_01b.sh` (#1417's, retargeted to the final body): **16 of 16 killed**, so every
    leg of 089's `fn_reaper_sweep` has a test that fails without it.
  - `content_schedule_02.sh` (phase 2): **14 of 14 killed** against the rewritten slot-key tests.
- **#1469** (the reaper's leg order and count): merged into this branch in a scratch worktree, never
  pushed. Its `TestTheSweepsLegOrder` passes 3 of 3 against 089's body, beside the per-leg tests
  (8 of 8) and the cancel leg's (7 of 7). Nothing needed updating.
- **The load flake:** the clock gate's `test_a_dead_clocks_election_is_released_by_its_session`
  lost its `pg_terminate_backend` race once at load ~15. Its leaked connection then failed a
  migration-gate case through a garbage-collected-connection warning. Both classes pass alone (8 of 8),
  and the full suite above did not hit it.
- **A Neon branch of production** (at 087; deleted afterwards, the deletion confirmed):
  - `python -m scripts.migration_runner apply` (the predeploy command), with 088 and 089 pending:
    applied in **1.6 s**, ledger 87 to 89, every postcondition true.
  - After: one slot key, `uq_intent_slot WHERE (origin = 'cadence')`. Both doors are owned by
    `svc_maintenance`, and the miss door is `STRICT`. The reaper carries 086's, 087's and 089's texts
    and all 8 legs. A draining worker's one-argument call resolves, and a NULL window lists no miss.
  - The lock reads, with the workspace scope, use `uq_lock_ws_scope` (221 locks in the copy).
    Without the scope, they are a sequential scan even with sequential scans switched off.
  - A planned story two hours past its time: the reaper left it alone, then one miss beat ended it
    `expired` with `late`, audited as `system`, with one pending notice in the right words.
  - A planned story due now: served, and its card names its scheduler.
  - Nothing was sent: no sender runs against a branch.
- `ruff check .` and `ruff format --check .` clean.

### Open for the owner (from review)

1. The contract's `DROP INDEX uq_intent_slot` takes `ACCESS EXCLUSIVE` on `post_intents` for the
   file's transaction, and the runner sets no `lock_timeout` (069 has the same shape). A long
   transaction on the ledger would hold the deploy, and the deploy would hold the ledger.
2. The web Queue does not show a miss's reason: its columns do not include `last_error`. The bound
   chats are told; a workspace with no chat sees only `expired`. Showing the reason is a product
   change.
3. The serve leg's `scheduled → prompt_pending` is not guarded on the flag. A removal committing
   between the door's read and the transition leaves a card for a flagged story until the reaper
   cancels it (the pipeline refuses a flagged story at admission, so nothing posts). A guarded serve
   would change main's serve path.
4. The notice and the planned line render in the workspace's zone, as the card's slot line does;
   other outcome lines use the account's zone when it has one.
5. "Can be served" is written twice in SQL: the serve door's allowlist and the miss door's `CASE`.
   The partition grid (every value the schema allows) holds them together; one SQL helper would be
   one source, at the cost of a new function and its grants.

## Context
- Source skill: forge · Area: `scripts/migrations/`, `src/services/target/prompts.py`, `src/services/target/work_loop.py` · Effort: L · Risk: High · Priority: High
