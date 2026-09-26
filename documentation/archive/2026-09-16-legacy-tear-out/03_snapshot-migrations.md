---
title: "Legacy tear-out — phase 03: the 3f snapshot migration, and the ratchet's rule for a file outside the advertised stream (PR 3)"
type: plan
status: completed
owner: chris
created: 2026-09-16
tags: [plan, legacy-retirement, migrations, data]
links: [https://github.com/chrisrogers37/storydump/issues/1216, https://github.com/chrisrogers37/storydump/issues/941]
---

> **Built 2026-09-18 as #1318 (`3ffa750`); 078 applied in production by the deploy of 17:24 UTC (ledger, phase 03).** The shipped names carry `20260917`, not this text's `20260916` placeholder; no `GRANT` bracket was needed (078's header).

## Summary

M.3 step 3f, owed since the window ran by hand: one runner migration that copies every table of
the `legacy` schema — the fourteen of `02` §9, `posting_history_dedup_archive` (#941) and
`schema_version` (both F4) — into `archive.<table>_pre_cutover_<date>` and hands each snapshot to
`svc_maintenance`, with a postcondition per table. It adds storage and drops nothing; the next
deploy applies it like any migration, and `storydump posture` shows it in the ledger. The date in
the names is the merge date; `20260916` below is the placeholder the PR replaces before merging.
Two things the file needs that the tree lacks ship with it: a rule by which the advertised-DDL
prefix ratchet excludes a file that acts on `legacy`/`archive` (today every file above the move
must be a prefix of the stream, and this one cannot be), and the runner's refusal of a
`runner:` marker it does not know (a misspelt marker is otherwise an ordinary file).

## Evidence

- `04-execution-sequence.md:199` — "3f the snapshot migrations — `CREATE TABLE
  archive.<t>_pre_cutover_<YYYYMMDD> AS TABLE legacy.<t>` + `ALTER … OWNER TO svc_maintenance`,
  every legacy table — after `archive` exists and before anything is dropped."
- `scripts/migrations/057_grant_matrix_and_archive_schema.sql` creates `archive` and grants
  `svc_maintenance` CREATE on it (`:74` postcondition `has_schema_privilege('svc_maintenance',
  'archive', 'CREATE')`).
- THE ACTOR. The runner connects with the DSN it is given and sets no role
  (`scripts/migration_runner.py:316-319`); production's `DATABASE_URL` is the database-owner
  login (`railway.toml`; `documentation/operations/migration-runner.md:88-94`), and every file
  through 077 was applied as that role — "the D40 privilege split was never armed" (`00` FC-7
  §7). So in production 078 runs as the OWNER, not as `svc_migration`. `OWNER TO svc_maintenance`
  needs membership of `svc_maintenance` (or superuser): the bootstrap grants it to `svc_migration`
  and `svc_migration` to the owner (`scripts/window/step0_bootstrap.sql:58,73`) — whether that
  bootstrap ever ran in production is unmeasured. CI's lane applies the post-move files as
  `svc_migration` (`tests/scripts/conftest.py:709-716`), a DIFFERENT actor from production's; the
  gate gains an owner-actor arm (`owner_actor`, `conftest.py:551`). Step 1 measures the actor,
  step 3 writes the file to the measurement.
- Legacy tables: `category_post_case_mix`, `audit_log`, `api_tokens`, `instagram_accounts`,
  `chat_settings`, `media_items`, `onboarding_sessions`, `posting_history`,
  `user_chat_memberships`, `media_posting_locks`, `service_runs`, `user_interactions`,
  `posting_queue`, `users` (`src/models/*.py` on `d8f5c72`, `__tablename__`), plus
  `posting_history_dedup_archive` and `schema_version` (the legacy ledger table — snapshot it too;
  it is data about the legacy lineage).
- THE PREFIX RATCHET. `scripts/advertised_ddl.py:307-316` `target_lineage_files` = every numbered
  file above the 051 move; `:319-334` `target_lineage_statements` = every statement of them;
  `tests/scripts/test_advertised_ddl.py:414-440` requires that list to be a positional prefix of
  the advertised stream, which `conftest.py:594-616` replays from an EMPTY `public` as
  `svc_migration` with no `legacy` schema; `tests/scripts/test_lineage_lane.py:95-123` slices the
  stream by the same length ("ONE HOME FOR THE PREFIX RULE"). A `CREATE TABLE archive.x AS TABLE
  legacy.x` inside the stream fails the replay; outside it, the prefix diverges. The manifest's
  `_comment` classifies `07` BLOCKS by hash and has no rule for a file; `test_advertised_ddl.py:290`
  pins 35 normative blocks. `target_lineage_statements`' own docstring anticipates "the first
  time the lineage needs any filtering — a data-only file, a marker-only file". This is that
  time: the rule is a marker on the file, read in ONE place.
- THE RUNNER'S MARKERS. `scripts/migration_runner.py:72-75` knows `no-transaction`,
  `postcondition`, `reapply-safe`, `schema-move`; `_parse_markers` (`:119-136`) ignores any other
  `runner:` line. `_load_manifest` (`:521-577`) needs no manifest entry for a file carrying
  `runner:postcondition` lines. A file with a ledger row never re-runs — so 078 needs no
  re-apply directive (the earlier hedge resolves to "none").
- THE CI WORLD. The lane's legacy world is `scripts/setup_database.sql` + 001–050
  (`test_lineage_lane.py:81-92`); `posting_history_dedup_archive` exists nowhere in the tree
  (grep: only this plan — it was made by hand in production, #941), so 078 against that world
  fails on "relation does not exist"; and only `schema_version` receives rows in that world
  (the INSERTs across 001–050), so a row-count postcondition reads 0 = 0 for fifteen tables and
  a `WITH NO DATA` mutation survives. The gate seeds both (step 4).
- Lineage list: `tests/scripts/test_lineage_lane.py:273-372` (append the file). The `07` block and
  the manifest row: NONE — the file is not advertised DDL; its postconditions are its adoption
  evidence and the pin at `:290` stays 35.

## Implementation Plan

### Dependencies
None of the code phases (the snapshot reads `legacy.*` whatever the code does); F4 ratified.
Merges after whichever of 01/02 is open by the one-PR-at-a-time policy, and its ratchet pins
(the manifest, the lineage list, `test_advertised_ddl`) are bumped against `main` at merge.

### Blocks
Phase 04.

### Steps

1. **Measure production first** (read-only). Agent-run through the sanctioned probe (the runtime
   login): `SELECT relname, pg_size_pretty(pg_total_relation_size(c.oid)), n_live_tup FROM
   pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace JOIN pg_stat_user_tables s ON s.relid
   = c.oid WHERE n.nspname = 'legacy' AND c.relkind = 'r'` — the table list and sizes, pasted;
   the list must be exactly the 16 of `LEGACY_TABLES` (phase 01's literal) or the plan is wrong
   and stops here; `SELECT version()`; `SELECT nspname, nspowner::regrole FROM pg_namespace WHERE
   nspname IN ('public','legacy','archive','window_ddl','runner')`; the `svc_*` membership graph
   both ways — `SELECT r.rolname AS role, m.rolname AS member, am.admin_option FROM pg_auth_members
   am JOIN pg_roles r ON r.oid = am.roleid JOIN pg_roles m ON m.oid = am.member WHERE r.rolname LIKE
   'svc\_%' OR m.rolname LIKE 'svc\_%'`; `SELECT rolname, rolbypassrls, rolcreaterole, rolsuper
   FROM pg_roles WHERE rolname NOT LIKE 'pg\_%'`; and `\d legacy.posting_history_dedup_archive`
   (its DDL is the gate's fixture in step 4). OWNER-run, because they ask about the runner's own
   login: `railway run --service worker -- python -m scripts.migration_runner status`, and under
   `DATABASE_URL`: `SELECT current_user, pg_has_role(current_user, 'svc_maintenance', 'MEMBER'),
   has_schema_privilege(current_user, 'archive', 'CREATE')`. Everything pasted into the PR; the
   owner's lines marked as such. Then the owner rehearses 078 on a PITR branch and records its
   wall-clock (the copy runs inside the predeploy; `railway.toml` gives the healthcheck 30 s,
   the predeploy its own budget — read it).
2. **The ratchet's file rule and the runner's marker check** (tests red first, in
   `tests/scripts/test_advertised_ddl.py`, `test_lineage_lane.py`, `test_migration_runner.py`):
   a new marker `-- runner:unadvertised` ("this file acts on `legacy`/`archive`; it is not part of
   the advertised DDL stream"). `scripts/migration_runner.py`: `_parse_markers` learns it (no
   behaviour change for `apply`) and RAISES `MigrationRunnerError` on any `runner:` line it does
   not know (the phase-04 `manual` marker is added there; a misspelling is refused at discovery).
   `scripts/advertised_ddl.py`: `target_lineage_files` excludes a file carrying `unadvertised` or
   `manual` — one definition, both consumers (`test_the_wired_prefix_holds_against_the_real_stream`
   builds its list from `target_lineage_files`; the lane's `_lineage_length` from
   `target_lineage_statements`, which calls it). Tests: the prefix test still passes with 078 in
   the tree; a positive control that the lane APPLIES an `unadvertised` file (078's ledger row
   after the lane); a unit test that a file with an unknown marker is refused; the pin at `:290`
   stays 35.
3. **`scripts/migrations/078_legacy_snapshots_pre_cutover.sql`** (`-- runner:unadvertised`) — for
   each of the sixteen tables, in `LEGACY_TABLES` order:
   ```sql
   CREATE TABLE archive.<t>_pre_cutover_20260916 AS TABLE legacy.<t>;
   ALTER TABLE archive.<t>_pre_cutover_20260916 OWNER TO svc_maintenance;
   -- runner:postcondition SELECT to_regclass('archive.<t>_pre_cutover_20260916') IS NOT NULL
   -- runner:postcondition SELECT (SELECT count(*) FROM archive.<t>_pre_cutover_20260916) = (SELECT count(*) FROM legacy.<t>)
   ```
   If step 1's `pg_has_role(current_user, 'svc_maintenance', 'MEMBER')` is false in production,
   the file brackets its `OWNER TO`s with `GRANT svc_maintenance TO current_user` at the top and
   `REVOKE` at the bottom (legal because the owner created the role — the `admin_option` column
   of step 1 confirms); if true, it does not. The header states: the obligation (`00` FC-7 §6,
   `04` 3f); that the name's date is the DROP-era date the `059:440-441` parser reads, not the
   cutover's (2026-08-24) — the shape is load-bearing for the retention class; the lifetime per
   F9; that the snapshots are read-only copies (no indexes, constraints, defaults or sequences —
   `CREATE TABLE … AS` copies none, and the legacy lineage declares no type a `DROP SCHEMA …
   CASCADE` could reach into `archive` through); that it runs unconditionally against sixteen
   named tables so an inventory error fails, never skips. The date is the merge date; update it
   in the PR before merging.
3b. **Lineage list**: append the file to `test_lineage_lane.py`'s list. No `07` block, no
   manifest row (the file rule of step 2 and the postconditions are why); the pin stays 35.
4. **Gate** — a new `tests/scripts/test_legacy_snapshots.py` built on the lane's world
   (`replayed_db`/`bootstrapped_db`, `conftest.py:749-760`): (i) create
   `legacy.posting_history_dedup_archive` from step 1's pasted `\d` (a fixture
   `tests/scripts/fixtures/legacy_by_hand.sql`, documented as production's hand-made table);
   (ii) insert one row per table as the owner, so every row-count postcondition compares 1 = 1,
   not 0 = 0; (iii) apply 078 in BOTH arms — as `svc_migration` (the lane's actor) and, in a second
   database, as the owner actor (production's actual actor; `pg_has_role` shaped as step 1
   measured — both memberships present, and absent with the bracket); (iv) assert every
   `archive.*_pre_cutover_<date>` exists, is owned by `svc_maintenance`, has its source's row
   count, and `svc_ingress` cannot read it (no grant); (v) a second `apply` is a no-op (the ledger
   row; no directive needed). `LEGACY_TABLES` is imported from `tests/scripts/legacy_inventory.py`
   (phase 01's literal) — if phase 01 has not merged yet, this PR creates the module and 01
   imports it.
5. **`storydump posture`** after the merge shows 078 applied; `storydump doctor` on a checkout at
   the merge shows the ledger matching. `storydump deploys --watch --commit <sha>` follows the
   deploy that applies it — the predeploy log's 078 line is the evidence the file ran as intended.

## Test Plan

- Red first: the runner's unknown-marker test and the ratchet's file-rule tests of step 2 (red
  because the runner ignores unknown markers and `target_lineage_files` filters nothing); the
  gate test of step 4 against the base (no such tables) fails.
- The replay gate (`tests/scripts/test_lineage_lane.py`) green with the new file in its list and
  applied by the lane; `test_advertised_ddl` green with the pin unchanged at 35.
- Battery `tests/mutations/legacy_tear_out_03.sh`: remove one table's statement (killed by the
  existence assertion); drop the `OWNER TO` (killed by the owner assertion); make the copy of
  `posting_history` `WITH NO DATA` (killed by the row-count postcondition, which compares 1 = 0
  on the seeded row); strip the `unadvertised` marker (killed by the prefix ratchet — the file
  enters the stream and diverges); misspell a marker (killed by the unknown-marker test); make
  `target_lineage_files` filter nothing (killed by the file-rule test).
- Deploy evidence: `storydump deploys --watch --commit <sha>`; `storydump posture` output pasted
  (the 078 row `applied`).

## Verification Checklist

- [ ] Pasted: the production table list from step 1 equals `LEGACY_TABLES`; the actor probe (`version()`, the membership graph, `nspowner`, `rolbypassrls`); the owner's `current_user`/`pg_has_role` lines; 078's wall-clock on the PITR branch.
- [ ] `python -m pytest tests/scripts/test_advertised_ddl.py tests/scripts/test_lineage_lane.py tests/scripts/test_legacy_snapshots.py -q` green; the pin at `:290` still reads 35.
- [ ] Owner-run, pasted: `storydump posture --json | jq '.data.migrations[-1]'` shows 078 applied after the deploy (the runner's own `status` needs the owner's DSN and says the same).
- [ ] Owner-run, pasted: the read-only probe after the deploy — 16 `archive.*_pre_cutover_<date>` tables owned by `svc_maintenance`, row counts equal to their sources.
- [ ] Owner-run, pasted: Neon's storage delta ≤ the sum of `pg_total_relation_size` over the 16 legacy tables measured in step 1 (the plan states no other ceiling; `05` §DR is a PITR window).

## What NOT To Do

- Do not snapshot into `public`, and do not add indexes or constraints to a snapshot.
- Do not write a `07` block or a manifest row for the file, and do not make it a prefix of the
  stream by any other trick (a `DO $$ … $$` wrapper, a guarded copy) — the file rule is the
  mechanism, once.
- Do not make 078 conditional on the sixteenth table existing — a missing table is an inventory
  error the gate must fail on.
- Do not drop, truncate or alter anything in `legacy` — that is phase 04 and the owner's hand.
- Do not run the migration against production by hand; the deploy's predeploy applies it.
- Do not edit an applied migration; a mistake gets a new numbered file.

## Context

Area: migrations, data, the runner · Effort: M · Risk: low (additive; the one production risk is a failing predeploy, mitigated by the owner-actor gate arm and the PITR rehearsal) · Priority: alongside 01/02, before 04.
