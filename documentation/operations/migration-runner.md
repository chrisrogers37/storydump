# Migration runner — operations

The numbered-SQL runner (`scripts/migration_runner.py`, plan §0.2 / #712).
Standalone: stdlib + psycopg2, zero `src` imports, addressed by
`DATABASE_URL` or `--database-url`. The connection string is never printed.

## Commands

```bash
python -m scripts.migration_runner apply      # apply every pending migration (a runner:manual file is owed, not applied)
python -m scripts.migration_runner apply --manual N   # apply ONE gated file by version — the operator's door (NEVER-run for agents)
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
ride into `legacy` mid-run. It supersedes the legacy lineage's own version
table — the one the 001–050 files stamp themselves into, with known gaps; a
replay still stamps it and the runner never reads it
(`adopt`'s docstring, `scripts/migration_runner.py`). In production that table rode into
`legacy` with the rest at the 051 move, is snapshotted as
`archive.schema_version_pre_cutover_20260917` (078), and was dropped with
`legacy` by 079 in the owner's window on 2026-09-19
([`legacy-window-close.md`](../archive/2026-09-16-legacy-tear-out/legacy-window-close.md)).

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
- `-- runner:schema-move` — the one lineage-boundary file (051): everything
  numbered below it is the legacy lineage, everything above the target's.
- `-- runner:unadvertised` — a file above the move that is NOT advertised DDL
  (a snapshot of `legacy` into `archive`, a drop, a stand-down): applied like
  any other, but left out of the F.2 prefix ratchet that diffs the lineage
  against the plan's stream (078 was the first; the tear-out, phase 03).
- `-- runner:manual` — a file the deploy must not run by itself (the legacy
  tear-out, phase 04; fork F6: 079 drops `legacy`, 080 stands the window
  down). `apply` skips it where it stands and prints `owed (manual) NNN`, exit
  0 — a deploy is never failed by a file that waits for an operator; `status`
  lists it the same way. It is EXEMPT from the below-head rule in both doors:
  ordinary files numbered above it keep applying, and `apply --manual NNN`
  applies it below the head, by name, exactly like any file — the advisory
  lock, the integrity check, one transaction with its postconditions and its
  ledger row. `--manual` refuses a version without the directive, a version
  not in the tree, and a version already recorded (a gated file runs once).
  The operator's sequence for 079 and 080 is recorded in
  `documentation/archive/2026-09-16-legacy-tear-out/legacy-window-close.md`; a later gated
  file is rehearsed on a branch first (`backup-restore.md`, *Rehearsing a change on a
  branch*) and applied from a checkout at the deployed commit (`worker-recovery.md`,
  *The checkout and the link*).
  `storydump doctor` reads the directive as well: a gated file the ledger lacks is
  reported as owed to the owner's window, not as a deployment behind the repository.
- A NEAR MISS — a comment opener followed straight by `runner` and a known word
  in a frame the grammar does not read (`--- runner:manual`, `-- -- runner:manual`,
  `/* runner:manual */`, `# runner:manual`, `-- runner manual`, `-- runner-manual`,
  `-- runner=manual`, a marker after code on its line) — is refused at discovery
  too. Each once read as prose, which for a `manual` file is the whole hazard. A
  byte-order mark at the start of a file is dropped before the first line is
  read; a file that is not UTF-8, or that holds a NUL byte (UTF-16 without its
  mark decodes as UTF-8 and its markers as prose), is refused by name. The rule
  binds prose too: a comment never OPENS with `runner` followed by a marker
  word, in any spelling — write "the runner's manual door", not
  `-- runner-manual files wait …`.
- Any other `-- runner:<word>` — a hard failure at discovery, naming the file.
  Every door (`apply`, `adopt`, `status`, `parity`) and the test suite's
  collection refuse the corpus until it is fixed: a misspelt marker (a stray
  space after the colon, a capital letter) would otherwise read as prose and
  make the file an ordinary one, applied at the next deploy — for a file that
  was meant to wait for an operator, that is the whole hazard. A flag marker
  takes no argument; a postcondition marker refuses to be bare.
- Legacy files that carry their own `BEGIN;`/`COMMIT;` run with psql
  semantics (statement-split), so post-commit `CREATE INDEX CONCURRENTLY`
  works exactly as it did by hand.
- New files (051+) should own no `BEGIN`/`COMMIT` — the runner wraps them,
  and the ledger row commits atomically with the DDL.

## Lock waits (#1515)

A wrapped file's lock waits are bounded. The runner sets `lock_timeout` to `LOCK_TIMEOUT` (1 s)
inside the file's own transaction (`set_config(..., true)`, gone at its commit). An `ALTER TABLE`
that cannot take ACCESS EXCLUSIVE within the bound fails with SQLSTATE 55P03. Without the bound it
would hold its place in the lock queue, and every later statement on the table, readers included,
would wait behind it for as long as the holder ran.

1 s keeps that stall under the app's own lock bound (the tap path's 2 s), so a statement queued
behind a waiting migration does not fail on its own.

The runner tries that failure again, and no other, after 1, 2, 4 and then 8 s
(`LOCK_RETRY_DELAYS_S`, about 20 s in all). Each retry is announced on stderr, as
`migration NNN (file): a lock was held past 1s; trying again in 1s`. A file that runs out of
attempts fails the apply like any failure: no ledger row, no partial change, and the deploy aborts
with the old version serving.

A retry re-runs the whole file. A file whose heavy work precedes its contended lock repeats that work,
so take the lock first.

The bound is transaction-local on purpose. A session-wide one would also bound the advisory lock
that the two services' predeploys queue on during a deploy.

A self-managed or no-transaction file runs without the bound, because its statements commit as they
go and a retry could not start clean. A file sets no `lock_timeout` of its own; the tenancy gate
refuses `SET`.

## Adoption — the 45-or-49 design

Production predates the ledger, migrations were applied by hand, and as of
2026-08-11 nobody could say whether 046–049 ever ran there (answered at first
contact, 2026-08-26 — below). `runner adopt` is built for exactly that: every
numbered file is paired with adoption evidence, one of three kinds —

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

**State as of 2026-09-02.** Steps 1–4 happened, though not as printed:
production entered the ledger on 2026-08-26 (`runner status`: 046–050
`adopted`, 051+ `applied`), and every migration through 066 was applied by
hand as the database-owner role rather than as `svc_migration` (#1195; the
window's actor story is in the plan, `00` FC-7 §7). The scheduled drift
monitor (#1197) reads live `information_schema` against the tree and was
clean at 066. Step 5 is armed by the PR that carries this paragraph:
`railway.toml`'s `preDeployCommand` runs `apply` on every deploy of either
service. Preconditions that PR states and the operator confirms before
merging: `DATABASE_URL` is set on both Railway services and is the
database-owner connection (the app's `TARGET_DATABASE_URL` is a separate,
runtime-only login), and `runner status` against production reports zero
pending. Rollback is reverting the line; the ledger is untouched either way.

The sequence as it was printed — a record of first contact, not a to-do:
production is in the ledger and the deploy is armed.

1. **Create the runner login** — as the database-owner actor (on Neon, the
   project's database owner), per the plan §0.2 login contract (the creator
   receives ADMIN on PG16+, which the M.3 bootstrap depends on):
   `CREATE ROLE svc_migration LOGIN PASSWORD '<out-of-band>';`
   plus, until the M.3 bootstrap owns broader grants:
   `GRANT ALL ON ALL TABLES IN SCHEMA public TO svc_migration;`
   `GRANT CREATE ON DATABASE <db> TO svc_migration;`
   (adopt probes read catalogs; apply executes DDL on public.)
2. **Pre-050 confirmation** — a `\d` of the two legacy tables 050's header
   names for this checkbox (`scripts/migrations/050_chain_reconciliation.sql:45-47`),
   against production, to confirm the file-derived residue analysis (plan
   §0.2 precondition; 050's DDL is defensive either way). Those tables live in
   `legacy` since the 051 move, so the step cannot be repeated as printed.
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
(`tests/scripts/test_migration_gate.py`): the legacy lineage replayed from
empty through the runner, up to the 051 move; adopt against
production-shaped fixtures at 45 and at 49; the tamper refusal; and the
schema-parity comparator's positive control. Its replayed-vs-models parity arm
went with the legacy models (the tear-out, phase 01); the comparator's
live-catalog path is exercised by `tests/scripts/test_lineage_lane.py`, which
replays the corpus across the move, and by
`tests/scripts/test_schema_drift_live.py`. The comparator still leaves one
table out by name — the legacy lineage's own version table
(`scripts/schema_parity.py`, `EXCLUDED_TABLES`) — which exists only on a
migration-built legacy schema.
