---
title: "Legacy tear-out — phase 03: the 3f snapshot migrations — every legacy table into archive (PR 3)"
type: plan
status: draft
owner: chris
created: 2026-09-16
tags: [plan, legacy-retirement, migrations, data]
links: [https://github.com/chrisrogers37/storydump/issues/1216, https://github.com/chrisrogers37/storydump/issues/941]
---

## Summary

M.3 step 3f, owed since the window ran by hand: one runner migration that copies every table of
the `legacy` schema — the fourteen of `02` §9 and `posting_history_dedup_archive` (#941, F4) —
into `archive.<table>_pre_cutover_20260916` and hands each snapshot to `svc_maintenance`, with a
postcondition per table. It adds storage and drops nothing; the next deploy applies it like any
migration, and `storydump posture` shows it in the ledger.

## Evidence

- `04-execution-sequence.md:199` — "3f the snapshot migrations — `CREATE TABLE
  archive.<t>_pre_cutover_<YYYYMMDD> AS TABLE legacy.<t>` + `ALTER … OWNER TO svc_maintenance`,
  every legacy table — after `archive` exists and before anything is dropped."
- `scripts/migrations/057_grant_matrix_and_archive_schema.sql` creates `archive` and grants
  `svc_maintenance` CREATE on it (`:74` postcondition `has_schema_privilege('svc_maintenance',
  'archive', 'CREATE')`).
- The runner applies files as `svc_migration`, which holds `SELECT ON ALL TABLES IN SCHEMA
  legacy` from the window bootstrap (`04:121-125`: the grants "ride the 3c rename" and die at 3g)
  and transient membership of `svc_maintenance` (`04:117-119`, revoked at step 8) — so `OWNER
  TO svc_maintenance` is legal for it until the stand-down. Verify in the replay gate, which
  seeds the bootstrap.
- Legacy tables: `category_post_case_mix`, `audit_log`, `api_tokens`, `instagram_accounts`,
  `chat_settings`, `media_items`, `onboarding_sessions`, `posting_history`,
  `user_chat_memberships`, `media_posting_locks`, `service_runs`, `user_interactions`,
  `posting_queue`, `users` (`src/models/*.py` on `d8f5c72`, `__tablename__`), plus
  `posting_history_dedup_archive` and `schema_version` (the legacy ledger table — snapshot it too;
  it is data about the legacy lineage).
- Ratchets: `tests/scripts/test_lineage_lane.py:273-372` (append `078_…`), `tests/scripts/
  test_advertised_ddl.py:290` (35 → 36 if the file ships a `07` block — it does not alter target
  tables; F.2's rule is that every migration in the lineage has a manifest row: read
  `scripts/advertised_ddl_manifest.json`'s `_comment` for whether an archive-only file needs a
  block, and follow it), `07-security-model.md` (a §24 block if required).
- The CI lineage replay seeds `legacy` (legacy setup + 001–050, then the 3c move) — the snapshot
  runs against real legacy tables in CI, not an empty schema.

## Implementation Plan

### Dependencies
Phase 02 merged (serialises the ratchet pins). F4 ratified.

### Blocks
Phase 04.

### Steps

1. **Measure production first** (read-only, the sanctioned probe): `SELECT relname,
   pg_size_pretty(pg_total_relation_size(c.oid)), n_live_tup FROM pg_class c JOIN pg_namespace n
   ON n.oid = c.relnamespace JOIN pg_stat_user_tables s ON s.relid = c.oid WHERE n.nspname =
   'legacy' AND c.relkind = 'r'` — the table list and sizes, pasted; the list must be exactly
   the 16 above (15 + `schema_version`) or the plan is wrong and stops here.
2. **`scripts/migrations/078_legacy_snapshots_pre_cutover.sql`** — for each table, in the
   order above:
   ```sql
   CREATE TABLE archive.<t>_pre_cutover_20260916 AS TABLE legacy.<t>;
   ALTER TABLE archive.<t>_pre_cutover_20260916 OWNER TO svc_maintenance;
   -- runner:postcondition SELECT to_regclass('archive.<t>_pre_cutover_20260916') IS NOT NULL
   -- runner:postcondition SELECT (SELECT count(*) FROM archive.<t>_pre_cutover_20260916) = (SELECT count(*) FROM legacy.<t>)
   ```
   with a header comment naming the obligation (`00` FC-7 §6, `04` 3f), the date, and that the
   snapshots are read-only copies (no indexes, no constraints — a snapshot, not a table in use).
   The date in the name is the merge date; update it in the PR before merging.
3. **Manifest / lineage / `07`**: append the file to `test_lineage_lane.py`'s list; the manifest
   row and `07` block per the manifest's own rule (step 5 of the evidence); bump the pin.
4. **Gate**: extend `tests/scripts/test_lineage_lane.py` (or a new `test_legacy_snapshots.py`):
   after the replay, every `archive.*_pre_cutover_20260916` exists, is owned by
   `svc_maintenance`, and has the same row count as its source; `svc_ingress` cannot read them
   (no grant); the file is idempotent under the runner's re-apply rule (read `runner:reapply`'s
   semantics — a snapshot must NOT re-run; if the runner would, the file carries the directive
   that prevents it, and the gate proves a second `apply` is a no-op).
5. **`storydump posture`** after the merge shows 078 applied; `storydump doctor` on a checkout at
   the merge shows the ledger matching.

## Test Plan

- Red first: the gate test of step 4 against the base (no such tables) fails.
- The replay gate (`tests/scripts/test_lineage_lane.py`) green with the new file.
- The DB gates untouched and green.
- Battery `tests/mutations/legacy_tear_out_03.sh`: remove one table's statement (killed by the
  existence assertion); drop the `OWNER TO` (killed by the owner assertion); make the copy `WITH
  NO DATA` (killed by the row-count postcondition).
- Deploy evidence: `storydump deploys --watch --commit <sha>`; `storydump posture` output pasted
  (the 078 row `applied`).

## Verification Checklist

- [ ] The production table list from step 1 equals the plan's list (pasted).
- [ ] `python -m scripts.migration_runner status` on a checkout at the merge: 078 pending before, applied after the deploy.
- [ ] `storydump posture --json | jq '.data.migrations[-1]'` shows 078 applied.
- [ ] The read-only probe after the deploy: 16 `archive.*_pre_cutover_20260916` tables owned by `svc_maintenance`, row counts equal to their sources (pasted).
- [ ] Neon storage after the copy within the plan's ceiling (`05` §DR floor; pasted).

## What NOT To Do

- Do not snapshot into `public`, and do not add indexes or constraints to a snapshot.
- Do not drop, truncate or alter anything in `legacy` — that is phase 04 and the owner's hand.
- Do not run the migration against production by hand; the deploy's predeploy applies it.
- Do not edit an applied migration; a mistake gets a new numbered file.

## Context

Area: migrations, data · Effort: M · Risk: low (additive) · Priority: after 02.
