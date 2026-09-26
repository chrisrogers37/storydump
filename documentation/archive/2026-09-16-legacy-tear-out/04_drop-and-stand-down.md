---
title: "Legacy tear-out — phase 04: the gated drop of the legacy schema and the window's stand-down (PR 4, owner-executed)"
type: plan
status: completed
owner: chris
created: 2026-09-16
tags: [plan, legacy-retirement, migrations, security, runbook]
links: [https://github.com/chrisrogers37/storydump/issues/1216, https://github.com/chrisrogers37/storydump/issues/1202, https://github.com/chrisrogers37/storydump/issues/410]
---

> **Built 2026-09-19 as #1321 (`c8482b3`); the owner ran the window the same evening** (079 in 1.341 s, 080 in 1.019 s; the gate green as printed) — the evidence is `RUN_LOG.md`, "The window — run by the owner on 2026-09-19", and the runbook `documentation/operations/legacy-window-close.md`.

## Summary

The irreversible half: `DROP SCHEMA legacy CASCADE` (M.3 step 3g) and the window's stand-down
(step 8, with the content F8 decides) — shipped as runner files the deploy cannot run by itself
(F6: a `-- runner:manual` directive the runner's `apply` skips and reports, exempt from the
below-head rule so a later ordinary file cannot wedge the deploys), with the window runbook the
owner follows after rehearsing on a Neon branch (M.2), and the identity-gated gate whose printed
output is the evidence. Both files run as the database owner, because that is what the runner
connects as. Merging this PR arms nothing; the owner runs the window (F7).

## Evidence

- `04-execution-sequence.md:200` — 3g is "the last runner file, gated on 3e's postconditions and
  the parity checks" (3e was abandoned by ruling; the parity checks are the snapshots' row
  counts). `:211-240` — the success variant: a subject-identity guard (`public.jobs` present AND
  `legacy` absent, else `RAISE`), `DROP SCHEMA IF EXISTS window_ddl CASCADE`, `REVOKE svc_claim,
  svc_clock, svc_maintenance, svc_membership FROM svc_migration`, `REVOKE svc_migration FROM
  <current_user>`, `REVOKE CREATE ON DATABASE … FROM svc_migration`, then the printed gate
  (identity first, then the steady-state shape).
- `04:93` — "The stand-down legs rehearsed below still govern 3g when it is scheduled";
  `../2026-08-17-m2-rehearsal-spec/README.md` — the rehearsal on a PITR branch.
- `railway.toml:19` `preDeployCommand = "python -m scripts.migration_runner apply"` — every
  pending file runs at the next deploy of either service. `scripts/migration_runner.py:72-75`
  knows four markers — `no-transaction`, `postcondition`, `reapply-safe`, `schema-move`;
  `_parse_markers` (`:119-136`) ignores an unknown `runner:` line (phase 03 makes that an error);
  subcommands `apply|adopt|repair|status|parity` (`:760-790`); there is no manual directive today.
- THE BELOW-HEAD RULE. `apply_pending` (`:479-487`) raises for ANY pending file numbered below
  the applied head unless it is `reapply-safe`, and it does so over the whole pending list
  BEFORE the apply loop (pinned by `tests/scripts/test_migration_runner.py:172`). With 079/080
  pending and one ordinary 081 merged (seventeen files landed in the last four weeks), the next
  predeploy of BOTH services raises on every push, and the owner's later `apply --manual 079`
  meets the same rule. A manual file must be exempt in both doors.
- THE ACTOR. `_connect` (`:316-319`) uses the DSN as given, no `SET ROLE`; production's
  `DATABASE_URL` is the database-owner login (`railway.toml`; `migration-runner.md:88-94`), so
  `apply --manual 080` runs as the owner, `current_user` in the stand-down resolves to the owner,
  and 080 CAN be a runner file. `repair` (`:696-706`) only UPDATEs an existing ledger row — the
  "run by hand, then `repair`" fallback cannot work and is gone from this plan.
- THE DOOR FILES. `ALTER FUNCTION … OWNER TO svc_*` recurs after the window (`062:166`,
  `063:194`, `064:71`, `068:145-147`, `076:81`) and needs membership of the new owner; the printed
  stand-down (`04:224-226`) revokes exactly those memberships. See F8 in the epic.
- No CI arm replays the success stand-down today: `grep -rn "success variant refused\|REVOKE
  svc_claim" tests/scripts` → nothing; `tests/scripts/test_window_bootstrap.py:113` guards the
  abandon variant only. `04:30` describes an intended gate, not the tree — step 5 writes it new.
- `tests/scripts/test_migration_gate.py:478` pairs every corpus file with adoption evidence: 080
  needs at least one `-- runner:postcondition` line (`_load_manifest`, `migration_runner.py:521`).
- `src/services/target/ops_views.py:355-363` — `posture`'s RLS list reads `public` only, so "no
  `legacy` table in `rls`" is vacuously true; the evidence that `legacy` is gone is 079's
  postcondition and the owner's `pg_namespace` query. Production is PostgreSQL 17.10 (#787), so
  the gate's PG16+ membership lines apply, not the PG15 ones.
- #1202: 3g "has no guard against running before #410's demo videos exist" — and, in its own
  words, "either leg satisfies it: record the Track 3 videos, or arm and verify the target tier".
  #410 OPEN (last comment 2026-08-19, nothing submitted); the target tier has served since
  2026-08-24. `documentation/operations/meta-app-review.md:32-47` is the standing constraint.
  The manual directive is a guard against an ACCIDENTAL deploy-time drop; it is not the
  #410-keyed refusal #1202 sketches, and this plan does not call it that.

## Implementation Plan

### Dependencies
Phase 03 merged and applied in production (every snapshot present, the actor probe pasted).
F6, F7 and F8 ratified. #1202 closed by the owner on its "or" leg — the target tier is armed and
serving, and the owner confirms a connected destination exists on it (the honest close; the
videos-first leg is #410's, not started) — BEFORE this PR merges; the runbook and
`meta-app-review.md` cite the ruling.

### Blocks
Phase 05's "legacy schema absent" statements; #941, #739 and, where they concern `legacy`-schema
instruments, #1046 and #1113 close with it (#945 closes in phase 01 as stale).

### Steps

1. **The runner learns `-- runner:manual`** (`scripts/migration_runner.py`): a file carrying the
   directive is listed by `status` as `owed (manual)`, skipped by `apply` with one printed line
   naming it, and applied by `apply --manual <version>` exactly like any file (ledger row,
   postconditions, advisory lock). A manual file is EXEMPT from the below-head rule in both
   doors: `apply` skips it where it stands even when later files have been applied, and
   `--manual` applies it below the head. Unit tests in `tests/scripts/test_migration_runner*.py`:
   skip; report; apply-by-name; refuse `--manual` for a version without the directive; refuse a
   misspelt marker (phase 03's error, if it has not merged first); and the two load-bearing
   ones — with a manual 079 pending, `apply` applies an ordinary 081 (numbered ABOVE it) and
   exits 0, and `apply --manual 079` then applies 079 below the head. One PR with the files: the
   manual-skip path is observable only once a manual file exists, so a runner-only PR would
   prove nothing these tests do not. Two obligations phase 03's grammar imposes, in the SAME
   commit as the first manual file: `manual` joins `KNOWN_MARKERS` in `scripts/migration_runner.py`
   (an unknown `runner:` word is refused at discovery — every door, and the test suite's
   collection, would refuse the corpus until it does), and `target_lineage_files` learns to
   exclude a manual file as it excludes an unadvertised one (else 079 enters the F.2 prefix
   diff and the ratchet goes red) — or 079/080 carry `-- runner:unadvertised` beside
   `-- runner:manual`, which is the same fact stated on the file.
2. **`scripts/migrations/079_drop_legacy_schema.sql`** (`-- runner:manual`):
   ```sql
   -- precondition, in-file: every snapshot exists with its source's row count
   DO $$ DECLARE t text; BEGIN
     FOREACH t IN ARRAY ARRAY['category_post_case_mix', …, 'posting_history_dedup_archive', 'schema_version'] LOOP
       IF to_regclass(format('archive.%I_pre_cutover_<the date 078 shipped with>', t)) IS NULL THEN
         RAISE EXCEPTION '3g refused: no snapshot for legacy.%', t;
       END IF;
     END LOOP;
   END $$;
   DROP SCHEMA legacy CASCADE;
   -- runner:postcondition SELECT NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'legacy')
   -- runner:postcondition SELECT count(*) = 16 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'archive' AND c.relname LIKE '%_pre_cutover_%'
   ```
   The table list is `LEGACY_TABLES` written out (the gate asserts the file's array equals the
   literal); the date is read off 078's file name at authoring time and stated in the header;
   `16` is the literal's length.
3. **The stand-down** — `scripts/migrations/080_window_stand_down.sql` (`-- runner:manual`), a
   runner file run by `apply --manual 080` as the owner (decided by the evidence above; no
   `psql` block, no `repair` fallback). Its content is F8's choice, written against phase 03's
   pasted membership graph: under F8 (a) — the subject-identity guard of `04:213-224` verbatim
   (`public.jobs` present AND `legacy` absent, else `RAISE`), `DROP SCHEMA IF EXISTS window_ddl
   CASCADE`, `REVOKE CREATE ON DATABASE … FROM svc_migration`, and NO revoke of a membership a
   door file needs; under (b) — the `04:213-240` block verbatim plus the door-file bracket rule
   written into `.claude/rules/database.md` and enforced by a corpus test (every post-080 file
   with `OWNER TO svc_*` carries its own `GRANT`/`REVOKE` bracket); under (c) — no 080 at all.
   Whichever: at least one `-- runner:postcondition` line (the identity — `legacy` absent AND
   `window_ddl` absent — so the manifest's adoption pairing is satisfied), and the gate queries
   as comments with their expected answers on the PG16+ branch (production is 17.10). The
   `03-decision-record.md` amendment F8 (a) requires ships in this PR, beside `03:189`.
4. **The runbook** `documentation/operations/legacy-window-close.md`: the M.2 rehearsal on a Neon
   PITR branch — the spec's 3g and step-8 legs only (`../2026-08-17-m2-rehearsal-spec/README.md`;
   3a–3d ran by hand in production and are not rehearsed again; the branch is the owner's to
   provision) — (branch, `apply --manual 079`, `apply --manual 080`, the gate, ONE
   door-replacing migration applied after the stand-down as the positive control for F8 — the
   next real door file if one is pending, else a throwaway copy of `076`'s `OWNER TO` statement
   on the branch — then the smoke reads with `storydump posture|health|floating`), then
   production, in this order and no other: **stop the worker service** (the backout is PITR to
   the marker, which discards every target write after it — a running worker would keep
   writing), announce, take the PITR marker, `railway run --service worker -- python -m
   scripts.migration_runner apply --manual 079`, `… apply --manual 080`, the gate's queries with
   their expected answers, `SELECT count(*) FROM pg_namespace WHERE nspname = 'legacy'` → 0,
   `storydump posture` showing 079/080 `applied` and no `window_ddl` door, restart the worker,
   and the backout (PITR to the marker; the worker still stopped) with its window. Every
   command as printed, no placeholders except the branch name. The runbook does not repeat the
   SQL: it names the files and the commands.
5. **Gate tests** — written NEW (no success arm exists in CI): `tests/scripts/
   test_window_close.py` on phase 03's world — the lane through 078, then `apply` alone leaves
   079 and 080 `owed (manual)` and applies nothing else (the mechanical guard against an
   accidental deploy-time drop); then `apply --manual 079` as the owner actor: `legacy` gone, the
   16 snapshots intact, the postconditions true; then `apply --manual 080` as the owner actor:
   the gate queries answer as the file's comments say, and — F8's positive control — one
   door-replacing statement (`ALTER FUNCTION fn_reaper_stale_approved(interval, int) OWNER TO
   svc_maintenance`, `076:81`'s) still succeeds as the owner afterwards; the refusal arms: 079
   with a snapshot deleted refuses and leaves `legacy` intact, 080 with `legacy` present refuses
   (the subject-identity guard). The lane's own end state gains "079 and 080 pending (manual)".
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
  a present `legacy` schema (killed by the guard test); re-add the below-head raise for manual
  files (killed by the 081-then-079 test); under F8 (a), add the membership revoke back (killed
  by the door-file positive control).
- The owner's evidence, pasted into the PR after the window: the rehearsal log with wall-clock,
  the production gate's five answers, `storydump posture --json` before and after.

## Verification Checklist

- [ ] `python -m scripts.migration_runner status` on a checkout at the merge lists 079 and 080 as `owed (manual)`; owner-run, pasted: the next deploy's predeploy log shows both skipped by name.
- [ ] Owner-run, pasted: the rehearsal on a Neon branch green end to end, wall-clock recorded, one door-replacing file applied after the stand-down.
- [ ] Owner-run, pasted: production — `SELECT count(*) FROM pg_namespace WHERE nspname = 'legacy'` → 0; the 16 snapshots present; the stand-down gate's queries answer as printed.
- [ ] Owner-run, pasted: `storydump posture` — 079/080 `applied`. (Its `doors` and RLS lists read `public` only — the step-0 door lived in its own schema to stay out of that census — so neither says anything about `window_ddl` or `legacy`; the `pg_namespace` lines above are the evidence. Corrected in phase 04's build: the first draft of this item asked `doors` for a door it never listed.)
- [ ] #1202 closed by the owner's ruling before the merge; `meta-app-review.md:32-47` rewritten in phase 05 to cite it.
- [ ] #941, #739 (and #1046/#1113 where they concern `legacy`) closed with references to this PR.

## What NOT To Do

- Do not merge without the manual directive proven in the gate — a pending 3g on `main` runs at
  the next push.
- Do not run 079 or the stand-down from an agent session; F7 (a): the owner runs the window.
- Do not drop `archive` or any snapshot; do not touch `public`.
- Do not "fix forward" a failed 3g by editing 079 — a refusal is the guard working; find the
  missing snapshot.
- Do not revoke a membership a door file needs (F8), and do not call the manual directive
  "#1202's guard" — it guards against a deploy, not against #410.
- Do not open the production window with the worker running.

## Context

Area: migrations, security, operations · Effort: L · Risk: high (irreversible; mitigated by the
guard, the rehearsal and PITR) · Priority: after 03, on the owner's schedule.
