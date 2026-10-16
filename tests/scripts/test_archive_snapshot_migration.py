"""The archive snapshot drop is bounded and idempotent."""

import re
import subprocess
import sys

import psycopg2
import pytest

from scripts.migration_runner import (
    MIGRATIONS_DIR,
    MigrationRunnerError,
    apply_manual,
    apply_pending,
    discover_migrations,
)
from tests.scripts.conftest import fetch_one


def snapshot_migration():
    return next(m for m in discover_migrations(MIGRATIONS_DIR) if m.version == 2)


def snapshot_names():
    return re.findall(
        r"DROP TABLE IF EXISTS archive\.([a-z_0-9]+);", snapshot_migration().sql
    )


def test_snapshot_drop_names_exactly_the_sixteen_tables():
    migration = snapshot_migration()
    names = snapshot_names()
    assert migration.execution_mode == "wrapped"
    assert len(names) == len(set(names)) == 16
    assert all("_pre_cutover_" in name for name in names)
    assert "CASCADE" not in migration.sql.upper()


@pytest.mark.parametrize("ambient_confirmation", [False, True])
def test_snapshot_drop_without_backup_confirmation_preserves_every_table(
    world_db, ambient_confirmation
):
    from psycopg2.extensions import make_dsn

    with psycopg2.connect(world_db) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM runner.schema_migrations WHERE version = 2")
        for name in snapshot_names():
            cur.execute(f'CREATE TABLE archive."{name}" AS SELECT 1 AS sentinel')

    runner_dsn = (
        make_dsn(world_db, options="-capp.archive_snapshot_backup_confirmed=on")
        if ambient_confirmation
        else world_db
    )
    with pytest.raises(MigrationRunnerError, match="exported or backed up"):
        apply_manual(runner_dsn, MIGRATIONS_DIR, 2)

    assert fetch_one(
        world_db,
        "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'archive' AND c.relname LIKE '%pre_cutover%'",
    ) == (16,)
    assert fetch_one(
        world_db, "SELECT count(*) FROM runner.schema_migrations WHERE version = 2"
    ) == (0,)


def test_snapshot_drop_preserves_other_archive_tables_and_repeats_cleanly(world_db):
    with psycopg2.connect(world_db) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM runner.schema_migrations WHERE version = 2")
        for name in snapshot_names():
            cur.execute(f'CREATE TABLE archive."{name}" AS SELECT 1 AS sentinel')
            cur.execute(f'ALTER TABLE archive."{name}" OWNER TO svc_maintenance')
        cur.execute("CREATE TABLE archive.audit_export_probe (sentinel integer)")
        cur.execute("INSERT INTO archive.audit_export_probe VALUES (37)")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.migration_runner",
            "--database-url",
            world_db,
            "apply",
            "--manual",
            "002",
            "--confirm-archive-backup",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "applied 002" in result.stdout
    assert fetch_one(
        world_db,
        "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'archive' AND c.relname LIKE '%pre_cutover%'",
    ) == (0,)
    assert fetch_one(world_db, "SELECT sentinel FROM archive.audit_export_probe") == (
        37,
    )
    assert apply_pending(world_db, MIGRATIONS_DIR).applied == []
