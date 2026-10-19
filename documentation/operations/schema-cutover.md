# Schema cutover

The deployment runner leaves `001_catalog_text.sql` and
`002_drop_archive_snapshots.sql` owed. An owner applies them separately. A
deployment must never decide when to remove archive snapshots.

The prerequisites below apply to the future owner-operated cutover. Running a
synthetic fixture proves the script's behavior; it does not prove that the
production database matches the schema code. Production cutover requires a
successful rehearsal of a current production dump with zero catalog differences.
The owner must also run the complete owner-applied sequence on a temporary Neon
branch of production before repeating it on production. The local dump workflow
below provides repeatable restore and parity checks; it does not replace that
branch rehearsal.

## Hold Railway deployments before integrating main

Railway automatically deploys `main`. Before integrating either the schema
foundation or the cutover-safety changes, the owner establishes a deployment
hold on both Railway services: `worker` and `storydump` (the API):

1. Pause automatic deployments for both services and hold manual deploys and
   redeploys as well.
2. Confirm no pre-deploy is in flight on either service; resolve queued or
   running deployment attempts before proceeding. An older pre-deploy runner
   must not race the ledger conversion.
3. Integrate the foundation and cutover-safety changes into `main` while the hold
   remains active. An intermediate foundation commit must not deploy by itself.
4. Use a checkout containing the supported `convert-ledger` command and the
   `-- runner:manual` markers in both `001_catalog_text.sql` and
   `002_drop_archive_snapshots.sql` for every rehearsal and owner cutover step.
   Record the checkout's commit before executing the commands below.

Keep both services' automatic deployments paused until all cutover changes are
integrated and the owner-applied migrations, separate constraint validation,
and final zero-difference parity check have completed. These are prerequisites
for the future owner-operated window; this document does not pause services,
integrate code, or execute a production action.

## Preserve the database and archive snapshots

Stop concurrent schema changes while taking the rehearsal artifacts. Use the
same PostgreSQL major version and compatible extensions for the dump and both
disposable clusters, with matching-major `pg_dump` and `pg_restore` clients.
The owner obtains these artifacts outside CI; none belongs
in Git. Set a restrictive file-creation mask and store them in the approved
backup location.

```sh
umask u=rwx,go=
pg_dump --format=custom --file=production-cutover.dump "$PRODUCTION_OWNER_DSN"
pg_dump --format=custom --file=archive-snapshots.dump \
  --table='archive.*_pre_cutover_*' "$PRODUCTION_OWNER_DSN"
pg_dumpall --roles-only --no-role-passwords \
  --dbname="$PRODUCTION_OWNER_DSN" --file=production-roles.sql
python -m scripts.schema_parity extract --read-only \
  --database-url "$PRODUCTION_OWNER_DSN" --out=production-catalog.json
pg_restore --list archive-snapshots.dump > archive-snapshots.contents
```

Check that the archive export contains all sixteen tables named by migration
002, and restore the backup into a disposable database to verify that it is
usable. Record the backup location, time, and successful restore. The full
rehearsal below restores the full backup including those snapshots. Refresh the
artifacts and repeat the rehearsal if the source catalog changes afterward.
`--confirm-archive-backup` is the operator's explicit acknowledgement that the
snapshots have been exported or backed up; the flag does not create a backup.

A database dump does not contain cluster roles. Preserve their attributes and
memberships with the password-free roles artifact. Do not strip object owners,
grants, role memberships, or database settings to make the comparison pass.
Protect the full database backup as production data.

## Prepare two independent disposable clusters

Provision two throwaway PostgreSQL clusters. Two databases in one cluster do
not isolate role drift: roles and memberships are cluster-wide. Never supply a
production DSN as either rehearsal administrator DSN. The script creates and
removes only its uniquely named databases, and creates/removes service roles on
the otherwise unused code cluster.
Reserve both clusters exclusively for this run; do not run other builders or
rehearsals on them concurrently.

On the restore cluster, the owner prepares the roles from
`production-roles.sql`, including the original database owner and all object
owners. Inspect the artifact before loading it, and account explicitly for the
bootstrap administrator already present on the disposable cluster. The script
does not execute the roles artifact or alter/remove any existing restore-cluster
role. All seven `svc_*` roles must already exist there. Restore ownership and
grants faithfully; missing roles must stop the restore.

On the code cluster, prepare the same database-owner login, with matching
attributes and settings, but no `svc_*` roles. The schema builder creates the
expected service roles and memberships there. Both administrator DSNs must use
the corresponding database-owner actor, which needs database/role creation
permissions, permission to restore the dump's ownership, and access to
`pg_control_system()` for the independent-cluster check. Configure disposable
authentication separately; the password-free roles artifact does not restore
passwords. The script never prints either DSN.

Match server versions, locale libraries, tablespaces, owner attributes, and
global role settings to the source. `pg_restore` into a uniquely named database
does not recreate source database-level metadata automatically. The script
creates the restore database with the source artifact's encoding, locale
provider, locale, tablespace, and connection limit, then restores its database
grants, grant options, and database-scoped role settings. These changes affect
only its newly created restore database. The code database retains its normal
schema-code defaults, so differences from those defaults still fail the final
comparison.

The mandatory source catalog artifact checks restore fidelity before any ledger
conversion or migration. A lost setting, grant, unsupported database property,
or different collation-library version must fail that check. Prepare the
disposable environment faithfully and repeat rather than editing the artifact
or weakening the comparison.

## Rehearse the cutover

Install the repository's Python dependencies and PostgreSQL client tools. Set
`REHEARSAL_RESTORE_ADMIN_DSN` and `REHEARSAL_CODE_ADMIN_DSN` to the two disposable
clusters, then run:

```sh
python -m scripts.cutover_rehearsal \
  --dump production-cutover.dump \
  --source-catalog production-catalog.json \
  --confirm-disposable-clusters \
  --confirm-archive-backup
```

The script performs these steps, stopping on the first error:

1. Verify independent cluster identifiers, source roles, and an empty service
   role namespace on the code cluster; create unique empty databases.
2. Restore the dump without `--no-owner` or `--no-privileges`, restore source
   database metadata from the catalog artifact onto the unique restore
   database, then require its catalog to match `production-catalog.json`
   before converting the ledger or applying migrations.
3. Build the expected database from current schema code and adopt it. Run the
   supported `convert-ledger --confirm-legacy-ledger` operation on the restored
   database: it atomically preserves the retired runner ledger as
   `runner.schema_migrations_legacy` and adopts the current chain using structural
   probes. It refuses an unknown or current-chain ledger, an existing legacy
   destination, a non-owner actor, or missing confirmation. Old row counts are
   not adoption evidence.
4. Preserve any public history tables without changing their records:
   `public.schema_migrations` becomes `runner.legacy_public_schema_migrations`
   and `public.schema_version` becomes `runner.legacy_schema_version`, where
   present. This helper never renames the runner ledger. An existing history
   destination is an error.
5. Run the ordinary pre-deploy `apply` and require zero applied with 001 and 002
   both owed. Run an initial diagnostic parity comparison against the expected
   database; pending owner-applied changes can legitimately differ at this point.
6. Run `apply --manual 001`; after its transaction commits, validate the two
   constraints in a separate transaction using
   `scripts/validate_catalog_constraints.sql`.
7. Run `apply --manual 002 --confirm-archive-backup`.
8. Run final `migration_runner parity` against the migrated restored database
   and require zero differences.

A final catalog difference, failed count floor, failed migration, or failed
restore exits nonzero. The initial comparison is diagnostic; it does not replace
the final gate. The final comparison must say `0 difference(s)` before cutover.
Out-of-scope roles and other reported metadata still require the owner's
inspection; zero compared differences does not mean that reported roles were
compared. The script removes its databases on success and failure. It removes
only service roles it created on the initially empty code cluster, leaving all
pre-existing restore-cluster roles intact.

The script also supports the required read-only drift gate. To audit a
separately prepared, migrated clone against a database built from schema code:

```sh
export SCHEMA_DRIFT_DSN="$MIGRATED_CLONE_DSN"
export REQUIRE_SCHEMA_DRIFT_CHECK=1
python -m scripts.cutover_rehearsal --check-drift --against "$SCHEMA_CODE_DSN"
```

With these environment variables set, the full dump rehearsal also performs
this additional read-only comparison against its own code-built database. A
required but missing DSN fails. Nothing restores, converts a ledger, adopts, or
applies a migration through `SCHEMA_DRIFT_DSN`; parity reads it in a read-only
transaction and rolls back. An unchanged production database can correctly
differ before its pending migrations are applied. Use the migrated clone for
the pre-cutover zero-difference proof.

The focused tests use the ordinary disposable test database configuration. The
complete dump/restore tests additionally need a second independent, otherwise
unused cluster with matching test-administrator attributes and no service roles:

```sh
export REQUIRE_TEST_DATABASE=1
export CUTOVER_REHEARSAL_CODE_DSN="$SECOND_DISPOSABLE_TEST_CLUSTER_DSN"
python -m pytest -o addopts= tests/scripts/test_cutover_rehearsal.py
python -m pytest -o addopts= tests/scripts/cutover_rehearsal_integration.py
```

The explicit integration file runs a production-shaped fixture with a legacy
ledger and all sixteen snapshots through the complete script, then repeats with
an extra public table and requires a failing parity result. Both paths check
database cleanup and unchanged restore-cluster roles and memberships. The file
is opt-in and is not collected by the default test filename pattern.
Run these as separate pytest processes so the focused suite's temporary owner
role and memberships are removed before comparing independent clusters.

## Rehearse on an owner-created Neon branch

The owner first creates a temporary Neon branch of the current production
branch, using the Neon console or an owner-operated CLI. Confirm the new branch
identity and its owner connection string before running any write. Set
`CUTOVER_BRANCH_DSN` to that temporary branch, and keep application traffic on
production. No step here creates a branch or changes production automatically.
The branch is the production-shaped rehearsal target, containing its real schema,
legacy ledger, and archive snapshots. Protect it as production data.

Build the schema-code reference on a matching Neon branch or an independent
cluster with the same PostgreSQL major version, role attributes, and owner and
provider membership edges as the rehearsal target. The parity comparison reports
membership grantors, but compares effective role-to-member edges and whether
those edges carry admin option and, on PostgreSQL 16 and later, the inherit and
set options; grantor names may differ. A snapshot saved before those options
were compared carries format version 1 and is refused; extract it again. When a
vanilla PostgreSQL reference does not model Neon provider memberships, adapt the
disposable reference to match the target's effective memberships. Do not grant
production roles new memberships or admin powers just to match that reference.

Prepare an empty code database on an independent disposable cluster using the
matching owner-role posture described above. Build and adopt it:

```sh
DATABASE_URL="$SCHEMA_CODE_DSN" python -m src.models.target.ddl build
python -m scripts.migration_runner --database-url "$SCHEMA_CODE_DSN" adopt
export CUTOVER_DSN="$CUTOVER_BRANCH_DSN"
```

Verify the current usable backup and snapshot export. The owner converts the
retired ledger with the supported command, preserves any public history tables,
then runs the normal pre-deploy path. The converter retains every old record at
`runner.schema_migrations_legacy`; it never repairs an unrecognized checksum or
infers the new schema from the retired version numbers. It adopts the new chain
in the same transaction as the rename, so an adoption failure preserves the
original ledger name and records.

```sh
python -m scripts.migration_runner --database-url "$CUTOVER_DSN" convert-ledger --confirm-legacy-ledger
python - <<'PY_OWNER'
import os
from scripts.cutover_rehearsal import preserve_public_legacy_ledgers

print(preserve_public_legacy_ledgers(os.environ["CUTOVER_DSN"]))
PY_OWNER
python -m scripts.migration_runner --database-url "$CUTOVER_DSN" apply
python -m scripts.migration_runner --database-url "$CUTOVER_DSN" parity --against "$SCHEMA_CODE_DSN"
```

Require `apply` to report zero applied with 001 and 002 both owed. Initial parity
is diagnostic: an exit indicating differences is expected for the pending
legacy-column, CHECK-constraint, or stored-function changes that 001 implements.
Inspect every reported difference against those declared changes before
continuing. A connection failure, failed count floor, unexplained table,
privilege, membership, or other catalog difference blocks the cutover. Archive
snapshots are reported separately; their removal still requires the explicit
backup acknowledgement. Initial parity is diagnostic and cannot satisfy the
final zero-difference requirement.

Run the owner-applied steps in order, after inspecting that initial report:

Before applying 001, count rows for which either replacement CHECK expression
is definitively false. PostgreSQL CHECK constraints accept `NULL`, so the query
uses `IS FALSE` rather than counting every row for which the expression is not
true. The block is read-only and does not change the database:

```sql
BEGIN READ ONLY;
SELECT
  (SELECT count(*) FROM public.post_intents
   WHERE (state <> 'posted'
          OR (published_via = 'manual' AND cap_consumed_on IS NOT NULL)
          OR (published_via = 'dry_run' AND publish_step = 'effect_confirmed' AND cap_consumed_on IS NOT NULL)
          OR (published_via = 'api' AND ig_container_id IS NOT NULL AND publish_step = 'effect_confirmed' AND cap_consumed_on IS NOT NULL)) IS FALSE)
    AS ck_posted_complete_violations,
  (SELECT count(*) FROM public.post_intents
   WHERE (published_via IN ('api','manual','dry_run')) IS FALSE)
    AS ck_intent_via_violations;
COMMIT;
```

Both counts must be zero before continuing. If either is nonzero, stop before
001 and have the database owner resolve the affected rows under the approved
data-repair process, then repeat this preflight. Keep the query results with
the branch identity and cutover record.

```sh
python -m scripts.migration_runner --database-url "$CUTOVER_DSN" apply --manual 001
psql "$CUTOVER_DSN" -X --set=ON_ERROR_STOP=1 \
  --single-transaction --file=scripts/validate_catalog_constraints.sql
python -m scripts.migration_runner --database-url "$CUTOVER_DSN" apply --manual 002 --confirm-archive-backup
python -m scripts.migration_runner --database-url "$CUTOVER_DSN" parity --against "$SCHEMA_CODE_DSN"
```

Migration 001 refuses non-null legacy queue references or `legacy_backfill`
intent rows. Its replacement CHECK constraints begin `NOT VALID`, enforcing new
writes immediately; the separate validation checks existing rows after the
exclusive-lock transaction has committed. Do not proceed to 002 or final parity
until both validations commit. Migration 002 refuses a missing acknowledgement
before any drop, then drops exactly its sixteen named tables without `CASCADE`.
Require the final command to exit zero and report zero differences before
approving a production cutover. Inspect out-of-scope roles and other reported
metadata as well. Save the initial and final reports with the branch identity
and backup evidence; a rehearsal result describes that exact branch state.

If validation fails after 001 commits, stop before 002. Migration 001 is already
committed, including its ledger record and `NOT VALID` constraints; do not rename
or repair the ledger to claim that 001 was undone. The database owner must
resolve the violating rows, then rerun
`psql "$CUTOVER_DSN" -X --set=ON_ERROR_STOP=1 --single-transaction
--file=scripts/validate_catalog_constraints.sql`. Continue only after both
validations commit. If the schema itself must be rolled back, use the approved
backup recovery procedure and verify the restored database; do not simulate a
rollback by editing migration history.

## Repeat on production

Only after the owner-created Neon branch rehearsal passes, the owner schedules
the production cutover, verifies a current usable backup/export, and prevents
concurrent schema changes. Refresh the branch and repeat the rehearsal if the
source schema has changed. During the approved owner-operated window, set:

```sh
export CUTOVER_DSN="$PRODUCTION_OWNER_DSN"
```

Repeat the same supported conversion, public-history preservation, ordinary
`apply`, initial diagnostic parity, manual 001, separate constraint validation,
backup-confirmed manual 002, and final parity sequence above. Stop on any
unexpected result. Require zero final differences again before switching
application traffic or enabling a deployment. Keep the privacy copy change local
until the production snapshot drop has been confirmed. A successful rehearsal
or an open PR does not authorize a production deployment.
Deploy only the fully integrated commit after the owner-applied steps complete
and final parity reports zero differences. At that point the owner may release
the deployment hold for both services and deploy that recorded commit; never
release an intermediate foundation deployment from the queue.

## Roll back the ledger conversion before owner-applied migrations

Before applying 001, the owner can reverse only the ledger conversion. Stop
concurrent migration invocations, connect to the same cutover database, and run
this transaction. It takes the runner's advisory lock, waits for any in-flight
owner migration, then requires exactly the adopted initial marker. It preserves
that new ledger as
`runner.schema_migrations_cutover_attempt` rather than deleting either history.
An existing saved-attempt destination is a hard error; inspect and preserve it
before another rollback attempt.

```sql
BEGIN;
SELECT pg_advisory_xact_lock(7120502026);
LOCK TABLE runner.schema_migrations IN ACCESS EXCLUSIVE MODE;
DO $rollback$
BEGIN
  IF to_regclass('runner.schema_migrations_legacy') IS NULL THEN
    RAISE EXCEPTION 'the preserved legacy ledger is missing';
  END IF;
  IF to_regclass('runner.schema_migrations_cutover_attempt') IS NOT NULL THEN
    RAISE EXCEPTION 'the saved cutover-attempt ledger already exists';
  END IF;
  IF (SELECT count(*) FROM runner.schema_migrations) <> 1
     OR EXISTS (SELECT 1 FROM runner.schema_migrations
             WHERE version <> 0 OR status IS DISTINCT FROM 'adopted') THEN
    RAISE EXCEPTION 'ledger rollback is allowed only before owner-applied migrations';
  END IF;
END $rollback$;
ALTER TABLE runner.schema_migrations RENAME TO schema_migrations_cutover_attempt;
ALTER TABLE runner.schema_migrations_legacy RENAME TO schema_migrations;
DO $public_history$
BEGIN
  IF to_regclass('runner.legacy_public_schema_migrations') IS NOT NULL THEN
    ALTER TABLE runner.legacy_public_schema_migrations SET SCHEMA public;
    ALTER TABLE public.legacy_public_schema_migrations RENAME TO schema_migrations;
  END IF;
  IF to_regclass('runner.legacy_schema_version') IS NOT NULL THEN
    ALTER TABLE runner.legacy_schema_version SET SCHEMA public;
    ALTER TABLE public.legacy_schema_version RENAME TO schema_version;
  END IF;
END $public_history$;
COMMIT;
```

This rollback changes ledger names and restores preserved public history tables;
it does not reverse schema changes. After 001 or 002 has run, use the verified
backup and the owner-approved recovery procedure. For a disposable Neon branch,
the owner can discard that branch and create a new branch from production to
rehearse again. Never rename a ledger backward to claim that applied DDL was
undone.
