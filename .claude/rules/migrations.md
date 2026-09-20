---
paths:
  - "scripts/migrations/**"
  - "scripts/migration_runner.py"
  - "scripts/advertised_ddl.py"
  - "scripts/advertised_ddl_manifest.json"
---

# Migrations

The runner is `scripts/migration_runner.py`; its operations page is
`documentation/operations/migration-runner.md` — read it before writing a file.
Every deploy of either service runs `apply` as its predeploy (`railway.toml:19`),
so **merged is applied**. The ledger is `runner.schema_migrations`.

- Name the file `scripts/migrations/NNN_description.sql` with the next number.
  No `BEGIN`/`COMMIT`: the runner wraps the file and its ledger row in one
  transaction. Files 001–050 are the legacy lineage, kept because the lineage
  lane replays them; do not edit them or add below 051.
- **An applied file is immutable** — the ledger stores its SHA-256 and a changed
  file fails every later apply. Fix forward with a new number (063 replacing
  062's `fn_clock_tick` is the precedent).
- Carry `-- runner:postcondition <SQL returning bool>` lines. They are also the
  file's permanent adoption probe, so assert only state this file creates, never
  an absence, and use `>=` for counts a later file will raise (058's header).
- Markers are a closed grammar (`migration_runner.py:76`-`:101`):
  `postcondition`, `no-transaction`, `reapply-safe`, `schema-move`,
  `unadvertised`, `manual`. A misspelt one is a hard failure at discovery, and a
  comment must not open with `runner` plus a marker word.
- A `-- runner:manual` file is owed by the deploy, never applied by it (079 and
  080 were, by the owner, in the window of 2026-09-19); `apply --manual <version>`
  is the owner's door and is in `CLAUDE.md`'s safety block.
- Schema DDL is **advertised**: the target lineage must stay an ordered prefix
  of the fenced `sql` blocks in the consolidated plan's `02-domain-model.md`
  and `07-security-model.md`, classified in
  `scripts/advertised_ddl_manifest.json`; which files count is one function,
  `target_lineage_files` (`scripts/advertised_ddl.py:307`).
  A new statement is appended to the plan and the manifest in the same PR, and
  the file is added to the ratified list at
  `tests/scripts/test_lineage_lane.py:270`. `-- runner:unadvertised` is for a
  file the stream's empty-database replay cannot hold — 078's snapshots, 079's
  drop, 080's stand-down — not a way around the ratchet.
- A migration that creates or alters a table changes the model in the same PR:
  lane parity compares the replayed `public` with `TargetBase.metadata.create_all`
  (`test_lineage_lane.py:799`). Use `columns.py`'s `TZ`, `NOW`, `GEN_UUID` and
  `pk()`. The CHECK lists the adapters share (`INTENT_STATES`, `PUBLISH_STEPS`,
  the audit kinds and channels, `TOKEN_ROLES`) are copied in `vocabulary.py` and
  pinned to their migration (`test_vocabulary.py:63`): change both together.
- A file that hands a function to a service role brackets the grant itself:
  `GRANT CREATE ON SCHEMA public TO svc_x; ALTER FUNCTION … OWNER TO svc_x;
  REVOKE CREATE ON SCHEMA public FROM svc_x` (`076_publish_wait_edges.sql:68`-`:83`).
- The schema these files build, its RLS posture and how services query it are
  `.claude/rules/database.md`. A new tenant table ships its `ENABLE ROW LEVEL
  SECURITY` and its policy in the same migration (the tenancy gate fails otherwise).
