"""Owner-confirmed conversion keeps retired migration history intact."""

import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import psycopg2
import pytest
from psycopg2.extensions import make_dsn

from scripts import migration_runner as runner
from src.config.settings import settings
from tests.scripts.conftest import fetch_all, fetch_one
from tests.scripts.test_archive_snapshot_migration import snapshot_names
from tests.scripts.test_catalog_text_migration import (
    base_world as base_world,
    schema_base_source as schema_base_source,
)


# Checksums of the retired files in the original repository history.
RETIRED_ROWS = (
    (1, "f75f8a5b999673786d5bc79fca214a6bc7cc7360104fc6ab7703d440f03767c1"),
    (2, "8e2d5502c1f1ba613c598982824f9588d94f8a3a7fc167df36f310d8cfc136bc"),
    (149, "82056a5dc500da085ff1ddbe33f0cff219a5d08b463bf8205fcca20159c0593c"),
)
MANIFEST = runner.MIGRATIONS_DIR / "adoption_manifest.json"
#: A version above every file in the tree: a ledger row no file stands for.
FUTURE_VERSION = (
    max(m.version for m in runner.discover_migrations(runner.MIGRATIONS_DIR)) + 1
)


def ledger_rows(dsn, table="schema_migrations"):
    assert table in {
        "schema_migrations",
        "schema_migrations_legacy",
        "schema_migrations_before_rollback",
    }
    return fetch_all(dsn, f"SELECT * FROM runner.{table} ORDER BY version")


@pytest.fixture
def retired_ledger(base_world):
    with psycopg2.connect(base_world) as conn, conn.cursor() as cur:
        cur.execute("DROP TABLE runner.schema_migrations")
        cur.execute(runner.LEDGER_DDL)
        for version, checksum in RETIRED_ROWS:
            cur.execute(
                "INSERT INTO runner.schema_migrations"
                " (version, checksum, applied_by, execution_ms, status)"
                " VALUES (%s, %s, current_user, %s, 'applied')",
                (version, checksum, version * 10),
            )
        for name in snapshot_names():
            cur.execute(f'CREATE TABLE archive."{name}" (sentinel integer)')
    return base_world


def convert(dsn, **kwargs):
    return runner.convert_ledger(dsn, runner.MIGRATIONS_DIR, MANIFEST, **kwargs)


@pytest.mark.parametrize("door", ["apply", "adopt"])
@pytest.mark.parametrize("bounded", [False, True])
def test_current_future_history_keeps_the_missing_file_diagnostic(
    world_db, door, bounded
):
    with psycopg2.connect(world_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO runner.schema_migrations (version, checksum, applied_by, status)"
            " VALUES (%s, 'future-current-migration', current_user, 'applied')",
            (FUTURE_VERSION,),
        )
    before = ledger_rows(world_db)
    kwargs = {"max_version": 0} if bounded else {}
    with pytest.raises(
        runner.MigrationRunnerError, match="no such file exists"
    ) as caught:
        if door == "apply":
            runner.apply_pending(world_db, runner.MIGRATIONS_DIR, **kwargs)
        else:
            runner.adopt(world_db, runner.MIGRATIONS_DIR, MANIFEST, **kwargs)
    assert "convert-ledger" not in str(caught.value)
    assert ledger_rows(world_db) == before


@pytest.mark.parametrize("door", ["apply", "adopt", "manual", "repair"])
def test_mixed_retired_collisions_cannot_be_repaired(world_db, door):
    with psycopg2.connect(world_db) as conn, conn.cursor() as cur:
        for version, checksum in RETIRED_ROWS[:2]:
            cur.execute(
                "UPDATE runner.schema_migrations SET checksum = %s WHERE version = %s",
                (checksum, version),
            )
    before = ledger_rows(world_db)
    with pytest.raises(runner.MigrationRunnerError, match="convert-ledger") as caught:
        if door == "apply":
            runner.apply_pending(world_db, runner.MIGRATIONS_DIR)
        elif door == "adopt":
            runner.adopt(world_db, runner.MIGRATIONS_DIR, MANIFEST)
        elif door == "manual":
            runner.apply_manual(world_db, runner.MIGRATIONS_DIR, 1)
        else:
            runner.repair(world_db, runner.MIGRATIONS_DIR, 1, "not conversion")
    assert "runner repair" not in str(caught.value)
    assert ledger_rows(world_db) == before


@pytest.mark.parametrize("door", ["apply", "adopt", "manual", "repair"])
def test_retired_history_refuses_and_points_at_conversion(retired_ledger, door):
    before = ledger_rows(retired_ledger)
    with pytest.raises(runner.MigrationRunnerError, match="convert-ledger") as caught:
        if door == "apply":
            runner.apply_pending(retired_ledger, runner.MIGRATIONS_DIR)
        elif door == "adopt":
            runner.adopt(retired_ledger, runner.MIGRATIONS_DIR, MANIFEST)
        elif door == "manual":
            runner.apply_manual(retired_ledger, runner.MIGRATIONS_DIR, 1)
        else:
            runner.repair(retired_ledger, runner.MIGRATIONS_DIR, 1, "not conversion")
    assert "runner repair" not in str(caught.value)
    assert ledger_rows(retired_ledger) == before


def test_conversion_preserves_every_old_field_and_adopts_the_current_base(
    retired_ledger,
):
    before = ledger_rows(retired_ledger)
    identity = fetch_one(
        retired_ledger,
        "SELECT oid, relowner, relacl::text FROM pg_class"
        " WHERE oid = 'runner.schema_migrations'::regclass",
    )
    report = convert(retired_ledger, confirm_legacy_ledger=True)

    assert ledger_rows(retired_ledger, "schema_migrations_legacy") == before
    assert (
        fetch_one(
            retired_ledger,
            "SELECT oid, relowner, relacl::text FROM pg_class"
            " WHERE oid = 'runner.schema_migrations_legacy'::regclass",
        )
        == identity
    )
    assert [migration.version for migration in report.adopted] == [0]
    assert [migration.version for migration in report.pending] == [1, 2, 3, 4, 5, 6]
    assert [(row["version"], row["status"]) for row in ledger_rows(retired_ledger)] == [
        (0, "adopted")
    ]
    pending = runner.apply_pending(retired_ledger, runner.MIGRATIONS_DIR, max_version=2)
    assert pending.applied == []
    assert [migration.version for migration in pending.owed] == [1, 2]
    # 003 runs after 001 and 002: the predeploy refuses it while they are owed.
    with pytest.raises(runner.MigrationRunnerError, match="003 runs after 001 and 002"):
        runner.apply_pending(retired_ledger, runner.MIGRATIONS_DIR)
    assert [(row["version"], row["status"]) for row in ledger_rows(retired_ledger)] == [
        (0, "adopted")
    ]
    assert [
        migration.version
        for migration in runner.adopt(
            retired_ledger, runner.MIGRATIONS_DIR, MANIFEST
        ).already
    ] == [0]


def test_a_second_conversion_refuses_without_changing_either_ledger(retired_ledger):
    convert(retired_ledger, confirm_legacy_ledger=True)
    current = ledger_rows(retired_ledger)
    legacy = ledger_rows(retired_ledger, "schema_migrations_legacy")

    with pytest.raises(
        runner.MigrationRunnerError, match="schema_migrations_legacy.*exists"
    ):
        convert(retired_ledger, confirm_legacy_ledger=True)

    assert ledger_rows(retired_ledger) == current
    assert ledger_rows(retired_ledger, "schema_migrations_legacy") == legacy


def test_renaming_before_manual_001_restores_the_retired_state(retired_ledger):
    before = ledger_rows(retired_ledger)
    convert(retired_ledger, confirm_legacy_ledger=True)
    with psycopg2.connect(retired_ledger) as conn, conn.cursor() as cur:
        cur.execute(
            "ALTER TABLE runner.schema_migrations RENAME TO schema_migrations_before_rollback"
        )
        cur.execute(
            "ALTER TABLE runner.schema_migrations_legacy RENAME TO schema_migrations"
        )

    assert ledger_rows(retired_ledger) == before
    assert [
        row["version"]
        for row in ledger_rows(retired_ledger, "schema_migrations_before_rollback")
    ] == [0]
    with pytest.raises(runner.MigrationRunnerError, match="convert-ledger"):
        runner.apply_pending(retired_ledger, runner.MIGRATIONS_DIR)


def test_conversion_requires_explicit_confirmation(retired_ledger):
    before = ledger_rows(retired_ledger)
    with pytest.raises(runner.MigrationRunnerError, match="confirm-legacy-ledger"):
        convert(retired_ledger)
    assert ledger_rows(retired_ledger) == before
    assert fetch_one(
        retired_ledger, "SELECT to_regclass('runner.schema_migrations_legacy')"
    ) == (None,)


def test_conversion_requires_the_database_owner_even_for_a_superuser(retired_ledger):
    before = ledger_rows(retired_ledger)
    administrator = make_dsn(
        retired_ledger, user=settings.DB_USER, password=settings.DB_PASSWORD
    )
    with pytest.raises(runner.MigrationRunnerError, match="database owner"):
        convert(administrator, confirm_legacy_ledger=True)
    assert ledger_rows(retired_ledger) == before


def test_conversion_refuses_an_existing_history_destination(retired_ledger):
    with psycopg2.connect(retired_ledger) as conn, conn.cursor() as cur:
        cur.execute("CREATE TABLE runner.schema_migrations_legacy (sentinel integer)")
        cur.execute("INSERT INTO runner.schema_migrations_legacy VALUES (37)")
    before = ledger_rows(retired_ledger)
    with pytest.raises(
        runner.MigrationRunnerError, match="schema_migrations_legacy.*exists"
    ):
        convert(retired_ledger, confirm_legacy_ledger=True)
    assert ledger_rows(retired_ledger) == before
    assert fetch_one(
        retired_ledger, "SELECT * FROM runner.schema_migrations_legacy"
    ) == (37,)


def test_conversion_refuses_a_version_above_the_retired_chain(retired_ledger):
    with psycopg2.connect(retired_ledger) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO runner.schema_migrations (version, checksum, applied_by, status)"
            " VALUES (150, 'unrecognized-history', current_user, 'applied')"
        )
    before = ledger_rows(retired_ledger)
    with pytest.raises(runner.MigrationRunnerError, match="above 149"):
        convert(retired_ledger, confirm_legacy_ledger=True)
    assert ledger_rows(retired_ledger) == before


def test_conversion_observes_a_writer_after_waiting_for_the_ledger_lock(retired_ledger):
    repeated_read = make_dsn(
        retired_ledger, options=r"-cdefault_transaction_isolation=repeatable\ read"
    )
    assert fetch_one(repeated_read, "SHOW default_transaction_isolation") == (
        "repeatable read",
    )
    writer = psycopg2.connect(retired_ledger)
    try:
        with writer.cursor() as cur:
            cur.execute(
                "INSERT INTO runner.schema_migrations (version, checksum, applied_by, status)"
                " VALUES (150, 'concurrent-history', current_user, 'applied')"
            )
        with ThreadPoolExecutor(max_workers=1) as pool:
            conversion = pool.submit(convert, repeated_read, confirm_legacy_ledger=True)
            deadline = time.monotonic() + 10
            try:
                while not fetch_one(
                    retired_ledger,
                    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE database ="
                    " (SELECT oid FROM pg_database WHERE datname = current_database())"
                    " AND relation = 'runner.schema_migrations'::regclass"
                    " AND mode = 'AccessExclusiveLock' AND NOT granted)",
                )[0]:
                    assert not conversion.done(), (
                        "conversion did not wait for the writer"
                    )
                    assert time.monotonic() < deadline, (
                        "conversion never waits on the ledger"
                    )
                    time.sleep(0.01)
                writer.commit()
                with pytest.raises(runner.MigrationRunnerError, match="above 149"):
                    conversion.result(timeout=10)
            finally:
                writer.rollback()
        assert fetch_one(
            retired_ledger,
            "SELECT count(*) FROM runner.schema_migrations WHERE version = 150",
        ) == (1,)
        assert fetch_one(
            retired_ledger, "SELECT to_regclass('runner.schema_migrations_legacy')"
        ) == (None,)
    finally:
        writer.close()


@pytest.mark.parametrize(
    "signature", ["version0", "checksum001", "checksum002", "filename"]
)
def test_conversion_refuses_any_current_chain_row(retired_ledger, signature):
    migrations = {
        m.version: m for m in runner.discover_migrations(runner.MIGRATIONS_DIR)
    }
    version = 0 if signature == "version0" else 17
    checksum = (
        migrations[int(signature[-3:])].checksum
        if signature.startswith("checksum")
        else "unrecognized-history"
    )
    with psycopg2.connect(retired_ledger) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO runner.schema_migrations (version, checksum, applied_by, status)"
            " VALUES (%s, %s, current_user, 'applied')",
            (version, checksum),
        )
        if signature == "filename":
            cur.execute("ALTER TABLE runner.schema_migrations ADD COLUMN filename text")
            cur.execute(
                "UPDATE runner.schema_migrations SET filename = '001_catalog_text.sql' WHERE version = 17"
            )
    before = ledger_rows(retired_ledger)
    with pytest.raises(runner.MigrationRunnerError, match="current chain"):
        convert(retired_ledger, confirm_legacy_ledger=True)
    assert ledger_rows(retired_ledger) == before


def test_failed_adoption_rolls_the_rename_back(retired_ledger, tmp_path):
    manifest = tmp_path / "adoption_manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "required_through": 0,
                "entries": [{"version": 0, "probe": "SELECT false"}],
            }
        )
    )
    before = ledger_rows(retired_ledger)
    with pytest.raises(runner.MigrationRunnerError, match="required applied"):
        runner.convert_ledger(
            retired_ledger, runner.MIGRATIONS_DIR, manifest, confirm_legacy_ledger=True
        )
    assert ledger_rows(retired_ledger) == before
    assert fetch_one(
        retired_ledger, "SELECT to_regclass('runner.schema_migrations_legacy')"
    ) == (None,)


def test_the_cli_converts_and_reports_adoption(retired_ledger):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.migration_runner",
            "--database-url",
            retired_ledger,
            "convert-ledger",
            "--confirm-legacy-ledger",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "runner.schema_migrations_legacy" in result.stdout
    assert "adopted 000" in result.stdout
    assert "pending 001" in result.stdout
    assert "pending 002" in result.stdout
