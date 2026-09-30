---
title: "[plan] Phase 2: the intent ledger learns 'planned' — content schedule"
type: plan
status: draft
owner: astrid
created: 2026-09-27
tags: [storydump, content-schedule, post-intents, migration, schema, issue-1413]
repos: storydump
---

# Phase 2 — The ledger learns `planned`

> **Built and merged as PR #1449** (branch `astrid/1413-p2-planned-origin`, squashed to main as
> `ae6bbb4d`): migration 088 and `07` §31, renumbered from 086 / §29 (see the Build notes). The SQL
> in step 1 below is the design as planned; the as-built SQL is
> `scripts/migrations/088_intent_origin_planned.sql`, and the Build notes at the end of this doc
> record where the build departed from it.

## Summary

Part of #1413. Size: M. `post_intents` gains an origin and the person who scheduled the row.
Planned rows are forced to manual approval, and only a person can approve one. The slot key gains
a cadence-only twin: this phase is the "expand" half of the index change, and phase 3 is the
"contract" half. No planned row can exist yet, because nothing creates one until phase 5, so
behaviour does not change for any customer.

## Evidence

- `post_intents` columns: `055:109-160`. Rows are born `scheduled` (the insert guard, `055:350-363`).
- `uq_intent_slot`: `055:246`, with no predicate.
- `plan_slot`'s insert, with its conflict target spelled as columns: `scheduler.py:482-486`.
- The guard trigger freezes terminal rows and checks edge legality, and nothing else
  (`055:306-323`).
- Person paths set `app.actor_kind = 'user'` (`telegram_dispatch.py:441`, `workspaces.py:170`);
  system paths set `'system'`; the accepted kinds are `055:199-200`.

## Implementation Plan

### Dependencies
The owner's approval of this DDL; F1, F2 and F6 locked.

### Blocks
Phase 3 (contract, serve and misses), phase 4 (the cap reads `origin`), phase 5.

### Steps
1. A new migration, taking the next free number (084 if nothing lands first), following the
   runner's header conventions (`documentation/operations/migration-runner.md`):

   ```sql
   ALTER TABLE post_intents
     ADD COLUMN origin TEXT NOT NULL DEFAULT 'cadence'
       CONSTRAINT ck_intent_origin CHECK (origin IN ('cadence','planned')),
     ADD COLUMN scheduled_by_user_id UUID NULL REFERENCES users(id) ON DELETE SET NULL,
     ADD CONSTRAINT ck_intent_planned_manual
       CHECK (origin = 'cadence' OR approval_mode = 'manual');

   -- Expand: the cadence-only slot key beside the old one. Phase 3 drops the old one.
   CREATE UNIQUE INDEX uq_intent_slot_cadence
     ON post_intents (workspace_id, ig_account_id, schedule_slot_at)
     WHERE origin = 'cadence';

   CREATE FUNCTION trg_intent_planned_person_approval() RETURNS trigger
   LANGUAGE plpgsql AS $$
   BEGIN
     IF NEW.origin = 'planned' AND OLD.state = 'awaiting_approval' AND NEW.state = 'approved'
        AND (COALESCE(current_setting('app.actor_kind', true), '') <> 'user'
             OR NULLIF(current_setting('app.actor_user_id', true), '') IS NULL) THEN
       RAISE EXCEPTION 'a planned story is approved only by a person (post_intent %)', OLD.id
         USING ERRCODE = 'check_violation';
     END IF;
     RETURN NEW;
   END $$;

   CREATE TRIGGER tg_intent_planned_person_approval BEFORE UPDATE OF state ON post_intents
     FOR EACH ROW EXECUTE FUNCTION trg_intent_planned_person_approval();
   ```

   The `DEFAULT 'cadence'` fills every existing row, and the new CHECK validates against them.
   Rehearse the migration on a Neon branch of production first, to confirm its lock time.
2. `scheduler.py:485`: spell the conflict target
   `ON CONFLICT (workspace_id, ig_account_id, schedule_slot_at) WHERE origin = 'cadence'`.
   Postgres infers any unique index that satisfies the predicate, partial or not, so the new
   spelling works while both indexes exist. Update the key-1 paragraph of the docstring
   (`scheduler.py:328-338`) to match.
3. Mirror the change in the SQLAlchemy models (`src/models/target/intent_ledger.py`): both
   columns, both CHECKs and the index. The mirror tests compare the models with the DDL.
4. Update the consolidated plan's advertised DDL (`02`, the table and its keys) in step.
5. Add a `CHANGELOG.md` entry.

## Test Plan

- Cadence idempotency is unchanged: a duplicate `plan_slot` still mints one intent.
- A planned row and a cadence row may share (workspace, account, instant); two cadence rows may
  not.
- The CHECK: inserting a planned row with `approval_mode = 'auto'` fails.
- The trigger: `awaiting_approval → approved` on a planned row raises under
  `actor_kind = 'system'`, and under `'user'` with no user id; it succeeds under `'user'` with a
  user id. A cadence row is unaffected under every actor.
- Every real approve path passes the trigger for a planned row: the Telegram tap, the web command
  route, and the CLI under a person token, each driven through its own adapter rather than a
  direct `UPDATE`.
- The deploy window: the old conflict spelling (no predicate) still mints during this phase,
  because both indexes exist.
- Revert check: each new test fails with its guard removed.

## Verification Checklist

- [ ] The migration applies on a fresh replay and on a production-shaped Neon branch.
- [ ] On that branch, `SELECT count(*) FROM post_intents WHERE origin <> 'cadence'` returns 0.
- [ ] The full suite passes, including the model-mirror tests and the replay gate.
- [ ] The approve-path tests pass through all three adapters.

## What NOT To Do

- Don't drop the old `uq_intent_slot` in this phase (overview, G6). The contract waits for phase 3.
- Don't apply the trigger to cadence rows. Auto mode is the owner's to decide, not this plan's.
- Don't widen the trigger past `awaiting_approval → approved`. The issue's rule is about
  approval, and "Posted myself" is already a person's own tap.

## Build notes (2026-09-29, PR #1449)

- **Three single-action `ALTER TABLE` statements**, not one: the tenancy gate refuses any `ALTER`
  with a top-level comma.
- **`origin` is fixed at birth by the same trigger**, and the rule reads `OLD.origin`. Keyed on a
  column one `UPDATE` could rewrite, the rule would not hold (`SET origin = 'cadence', state =
  'approved'` would walk past it).
- **The trigger's `WHEN` is the rule's scope** (`tg_intent_audit`'s pattern), so a cadence row's
  update never runs the function; the body checks only the origin and the actor.
- **Names:** `trg_intent_planned_person` and `tg_intent_planned_person`, since they guard `origin`
  as well as approval.
- **Numbering:** built as 086 / `07` §29. Under the owner's landing order (no PR stacked on
  another), #1442 (085), #1452 (086) and #1417 (087) landed first. This phase merged main in and
  became **088 / `07` §31 / manifest ordinal 29**, renumbered only in the lines it adds, since main's
  own 086 and §29 are #1429's. The runner refuses a pending file below the applied head, so the
  order is enforced at merge.
- **Changed for phase 3:** 086's adoption probe matched the index by name. Phase 3 renames that
  index, and `runner adopt` refuses a false probe below a true one, so the probe now matches the
  index's definition.
- **The stated interim:** while both slot keys exist, the unconditional one still decides, so a
  planned row holds its instant against a cadence mint. Pinned by a test; phase 3's contract lifts it.
- **Proven before the PR opened:** every real approve path (the tap, the web, the CLI) approves a
  planned story through the trigger; a 14-mutation battery
  (`tests/mutations/content_schedule_02.sh`) kills all 14 through their named tests; and a Neon
  branch of production applied 086 through the predeploy command in 1.1 s (ledger 84 to 86).

## Context
- Source skill: forge · Area: `scripts/migrations/`, `src/services/target/scheduler.py`, `src/models/target/` · Effort: M · Risk: Medium · Priority: High
