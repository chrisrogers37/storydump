# Migration runner — operations

The numbered-SQL runner (`scripts/migration_runner.py`, plan §0.2 / #712).
Standalone: stdlib + psycopg2, zero `src` imports, addressed by
`DATABASE_URL` or `--database-url`. The connection string is never printed.

## Commands

```bash
python -m scripts.migration_runner apply      # apply every pending migration
python -m scripts.migration_runner adopt      # enter a pre-ledger DB into the ledger
python -m scripts.migration_runner status     # read-only ledger vs tree report
python -m scripts.migration_runner repair --version N --reason "…"
python -m scripts.migration_runner parity --against <dsn>   # schema diff, exit 1 on drift
```

`--migrations-dir` defaults to `scripts/migrations`; `adopt --manifest`
defaults to `scripts/migrations/adoption_manifest.json`.

## The ledger

`runner.schema_migrations(version PK, checksum, applied_at, applied_by,
execution_ms, status ∈ {applied, repaired, adopted})`, created by the runner
at first contact. It lives in the dedicated `runner` schema — never `public`
— because the M.3 cutover renames `public` wholesale and the ledger must not
ride into `legacy` mid-run. It supersedes the legacy `schema_version` table
(which keeps its in-file self-stamps until it is archived at M.3, but is
never read by the runner).

Checksums are SHA256 of the file bytes. An applied file that no longer
matches its recorded checksum is a hard failure everywhere: fix forward with
a new migration, or record a deliberate exception with `repair` (the reason
is mandatory and lands in the run log; the ledger row flips to `repaired`
with the new checksum).

## File conventions

- `-- runner:postcondition <SQL returning bool>` — executed after the file
  applies; anything but a single `true` fails the migration.
- `-- runner:no-transaction` — statement-by-statement autocommit execution
  (required for `CREATE INDEX CONCURRENTLY`). Such files must be idempotent.
- `-- runner:reapply-safe` — idempotent data migrations whose applied state
  is undecidable in place (048): adopt may leave them pending below an
  adopted head, and apply re-runs them there.
- Legacy files that carry their own `BEGIN;`/`COMMIT;` run with psql
  semantics (statement-split), so post-commit `CREATE INDEX CONCURRENTLY`
  works exactly as it did by hand.
- New files (051+) should own no `BEGIN`/`COMMIT` — the runner wraps them,
  and the ledger row commits atomically with the DDL.

## Adoption — the 45-or-49 design

Production predates the ledger, migrations were applied by hand, and as of
2026-08-11 nobody can say whether 046–049 ever ran there. `runner adopt` is
built for exactly that: every numbered file is paired with adoption
evidence, one of three kinds —

- an explicit **probe** in the manifest (SQL returning bool);
- an explicit **asserted** entry (data-only files with no structural delta:
  018, 022, 024, 027, 036, 039, 044) — legal only at or below the floor, and
  reported on its own `asserted NNN` line so the operator approving first
  contact can see which entries are evidence and which are declared trust;
- **derived from the file's own `runner:postcondition` lines** when it has
  them (050 onward) — one predicate, one home, so probe and postcondition
  cannot drift.

Files at or below `required_through` (045) must read true or adopt
hard-fails naming the version and probe. Above the floor, true adopts and a
contiguous false tail stays pending for a gated apply — so the same
invocation is correct at 45, at 49, or anywhere coherent between, with no
foreknowledge. A false below a true (an incoherent chain) refuses unless the
file is `reapply-safe`. A probe that *errors* is a hard failure, never
treated as false. A failed adopt writes nothing. An asserted entry above the
floor refuses at manifest load — above the floor, trust is never the
mechanism.

The probes are read-only. Answering "did 046–049 apply to prod?" therefore
needs no migration run: `runner status` + `adopt` against production report
it mechanically the moment access exists. `runner parity --against <dsn>`
gives the operator the CI gate's schema comparison against any live pair
(e.g. production vs a freshly replayed scratch database) with the same
comparator CI uses.

## Production rollout — every step human-gated

Ground rule: no production migration runs before this runner ships, and
none of this is armed by merging the PR that adds it.

1. **Create the runner login** — as the database-owner actor (on Neon, the
   project's database owner), per the plan §0.2 login contract (the creator
   receives ADMIN on PG16+, which the M.3 bootstrap depends on):
   `CREATE ROLE svc_migration LOGIN PASSWORD '<out-of-band>';`
   plus, until the M.3 bootstrap owns broader grants:
   `GRANT ALL ON ALL TABLES IN SCHEMA public TO svc_migration;`
   `GRANT CREATE ON DATABASE <db> TO svc_migration;`
   (adopt probes read catalogs; apply executes DDL on public.)
2. **Pre-050 confirmation** — `\d api_tokens` and `\d media_posting_locks`
   against production to confirm the file-derived residue analysis
   (plan §0.2 precondition; 050's DDL is defensive either way).
3. **First contact** — `runner adopt` with `DATABASE_URL` set to production.
   Expect: 001–045 adopted; 046–049 adopted or pending depending on what the
   hand-applied history actually was; 050 pending. Any hard failure names the
   discrepancy — resolve with a human, never by editing the manifest to make
   it pass.
4. **Gated apply** — if 048 is in the pending tail (step 3 explicitly
   anticipates this), first run its pre-flight precondition query (in the
   048 file header) against production and pause on a large deviation from
   the recorded snapshot — that snapshot is from 2026-07-20 and has never
   been re-run against current production. Then `runner apply` applies
   whatever adopt left pending (046–050 at most, 050 at least).
5. **Arm the deploy pipeline** — uncomment `preDeployCommand` in
   `railway.toml` (both services; the advisory lock serializes them). From
   then on merged == applied, and the deploy fails closed if a migration
   errors.

## CI

The migration gate runs inside the ordinary pytest suite
(`tests/scripts/test_migration_gate.py`): full-corpus replay-from-empty
through the runner, adopt against production-shaped fixtures at 45 and at
49, the tamper refusal, and the schema-parity comparator
(replayed == models, with `schema_version` as the one documented exclusion).
