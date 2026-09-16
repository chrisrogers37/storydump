---
title: "Legacy tear-out — phase 04: the gated drop of the legacy schema and the window's stand-down (PR 4, owner-executed)"
type: plan
status: draft
owner: chris
created: 2026-09-16
tags: [plan, legacy-retirement, migrations, security, runbook]
links: [https://github.com/chrisrogers37/storydump/issues/1216, https://github.com/chrisrogers37/storydump/issues/1202, https://github.com/chrisrogers37/storydump/issues/410]
---

## Summary

The irreversible half: `DROP SCHEMA legacy CASCADE` (M.3 step 3g) and the success-path
privilege stand-down (step 8) — shipped as runner files the deploy cannot run by itself (F6: a
`-- runner:manual` directive the runner's `apply` skips and reports), with the window runbook
the owner follows after rehearsing on a Neon branch (M.2), and the identity-gated success gate
whose printed output is the evidence. Merging this PR arms nothing; the owner runs the window
(F7).

## Evidence

- `04-execution-sequence.md:200` — 3g is "the last runner file, gated on 3e's postconditions and
  the parity checks" (3e was abandoned by ruling; the parity checks are the snapshots' row
  counts). `:211-240` — the success variant: a subject-identity guard (`public.jobs` present AND
  `legacy` absent, else `RAISE`), `DROP SCHEMA IF EXISTS window_ddl CASCADE`, `REVOKE svc_claim,
  svc_clock, svc_maintenance, svc_membership FROM svc_migration`, `REVOKE svc_migration FROM
  <current_user>`, `REVOKE CREATE ON DATABASE … FROM svc_migration`, then the printed gate
  (identity first, then the steady-state shape).
- `04:93` — "The stand-down legs rehearsed below still govern 3g when it is scheduled";
  `2026-08-17-m2-rehearsal-spec/README.md` — the rehearsal on a PITR branch.
- `railway.toml:19` `preDeployCommand = "python -m scripts.migration_runner apply"` — every
  pending file runs at the next deploy of either service. `scripts/migration_runner.py` knows
  `runner:postcondition`, `runner:reapply`, `runner:schema` and `runner:no…` directives
  (`:460 apply_pending`, `:589 adopt`, `:720 status`, subcommands `apply|adopt|repair|status`);
  there is no manual directive today.
- #1202: 3g "has no guard against running before #410's demo videos exist"; #410 OPEN.
- The stand-down revokes memberships from `svc_migration`, which is the runner's own login
  (`04:25`): the block must run as the database owner (`neondb_owner`, the `DATABASE_URL` both
  services carry, `railway.toml:14-16`), not through the runner's `svc_migration` session. So:
  3g is a manual runner file; the stand-down is a manual runner file only if the runner can run
  it as the owner — read `migration_runner.py`'s connection handling; otherwise it is a runbook
  block the owner runs with `psql` as the owner, and its gate queries are the evidence.

## Implementation Plan

### Dependencies
Phase 03 merged and applied in production (every snapshot present). F6 and F7 ratified. #1202's
condition met — the owner states in the PR that #410's videos exist or the target tier records
them.

### Blocks
Phase 05's "legacy schema absent" statements; #941, #945, #739 close with it.

### Steps

1. **The runner learns `-- runner:manual`** (`scripts/migration_runner.py`): a file carrying the
   directive is listed by `status` as `owed (manual)`, skipped by `apply` with one printed line
   naming it, and applied by `apply --manual <version>` exactly like any file (ledger row,
   postconditions, advisory lock). Unit tests in `tests/scripts/test_migration_runner*.py`: skip,
   report, apply-by-name, refuse `--manual` for a version without the directive, and — the
   load-bearing one — `apply` with a manual file pending applies everything ELSE and exits 0.
2. **`scripts/migrations/079_drop_legacy_schema.sql`** (`-- runner:manual`):
   ```sql
   -- precondition, in-file: every snapshot exists with its source's row count
   DO $$ DECLARE t text; BEGIN
     FOREACH t IN ARRAY ARRAY['category_post_case_mix', …, 'posting_history_dedup_archive', 'schema_version'] LOOP
       IF to_regclass(format('archive.%I_pre_cutover_20260916', t)) IS NULL THEN
         RAISE EXCEPTION '3g refused: no snapshot for legacy.%', t;
       END IF;
     END LOOP;
   END $$;
   DROP SCHEMA legacy CASCADE;
   -- runner:postcondition SELECT NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'legacy')
   -- runner:postcondition SELECT count(*) = 16 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'archive' AND c.relname LIKE '%_pre_cutover_20260916'
   ```
3. **The stand-down** — `scripts/migrations/080_window_stand_down.sql` (`-- runner:manual`) if
   the runner can execute it as the owner (step 1 decides: if `apply --manual` connects as
   `DATABASE_URL`'s owner already, the file is fine; the `REVOKE svc_migration FROM
   current_user` line then names the owner correctly), else the same statements as a fenced
   block in the runbook with the ledger row written by `repair --version 080 --reason "run by
   hand as neondb_owner"` after. The block is the `04:213-240` text verbatim, including the
   subject-identity guard and the printed gate queries.
4. **The runbook** `documentation/operations/legacy-window-close.md`: the M.2 rehearsal on a Neon
   PITR branch (branch, `apply --manual 079`, the stand-down, the gate, the smoke reads with
   `storydump posture|health|floating`), then production: announce, the PITR marker, `railway
   run --service worker -- python -m scripts.migration_runner apply --manual 079`, the
   stand-down, the gate's five queries with their expected answers, `storydump posture` showing
   `legacy` gone from the RLS table list and 079/080 in the ledger, and the backout (PITR to the
   marker) with its window. Every command as printed, no placeholders except the branch name.
5. **Gate tests**: extend the lineage replay to run 079 and 080 by name after the snapshots and
   assert the postconditions and the printed gate (the M.2 spec's success arm already exists in
   CI per `04:30` — reuse it, do not duplicate); assert that `apply` alone leaves 079 and 080
   pending (the mechanical guard #1202 asks for).
6. **The never-run lists**: `python -m scripts.migration_runner apply --manual` joins
   `CLAUDE.md`/`AGENTS.md`'s block (both files; `tests/test_agent_docs.py` keeps them equal —
   note the block's grammar is `storydump …` and `python -m …` lines; the satellite pin covers
   `storydump` verbs only, so the two `.claude` files get the line by hand).

## Test Plan

- Red first: the runner tests of step 1; the replay gate asserting 079/080 stay pending under
  plain `apply` (red because the runner applies everything today) and drop the schema under
  `--manual` (red because the files do not exist).
- Battery `tests/mutations/legacy_tear_out_04.sh`: strip the `runner:manual` directive from 079
  (killed by the stays-pending gate); remove one snapshot name from 079's precondition (killed by
  a gate that deletes that snapshot and expects the refusal); make the stand-down's guard pass on
  a present `legacy` schema (killed by the guard test).
- The owner's evidence, pasted into the PR after the window: the rehearsal log with wall-clock,
  the production gate's five answers, `storydump posture --json` before and after.

## Verification Checklist

- [ ] `python -m scripts.migration_runner status` on `main` after the merge: 079 and 080 `owed (manual)`; the next deploy's predeploy log shows both skipped by name.
- [ ] Rehearsal on a Neon branch green end to end, wall-clock recorded.
- [ ] Production: `SELECT count(*) FROM pg_namespace WHERE nspname = 'legacy'` → 0; the 16 snapshots present; the stand-down gate's queries answer as printed (pasted by the owner).
- [ ] `storydump posture`: no `legacy` table in `rls`, no `window_ddl` door in `doors`, 079/080 `applied`.
- [ ] #941, #945, #739 closed with references to this PR.

## What NOT To Do

- Do not merge without the manual directive proven in the gate — a pending 3g on `main` runs at
  the next push.
- Do not run 079 or the stand-down from an agent session; F7 (a): the owner runs the window.
- Do not drop `archive` or any snapshot; do not touch `public`.
- Do not "fix forward" a failed 3g by editing 079 — a refusal is the guard working; find the
  missing snapshot.

## Context

Area: migrations, security, operations · Effort: L · Risk: high (irreversible; mitigated by the
guard, the rehearsal and PITR) · Priority: after 03, on the owner's schedule.
