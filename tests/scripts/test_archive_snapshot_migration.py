"""The archive snapshot drop is bounded and idempotent."""

import re

import psycopg2

from scripts.migration_runner import MIGRATIONS_DIR, apply_pending, discover_migrations
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


def test_snapshot_drop_preserves_other_archive_tables_and_repeats_cleanly(world_db):
    with psycopg2.connect(world_db) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM runner.schema_migrations WHERE version = 2")
        for name in snapshot_names():
            cur.execute(f'CREATE TABLE archive."{name}" AS SELECT 1 AS sentinel')
            cur.execute(f'ALTER TABLE archive."{name}" OWNER TO svc_maintenance')
        cur.execute("CREATE TABLE archive.audit_export_probe (sentinel integer)")
        cur.execute("INSERT INTO archive.audit_export_probe VALUES (37)")
    report = apply_pending(world_db, MIGRATIONS_DIR)
    assert [migration.version for migration in report.applied] == [2]
    assert fetch_one(
        world_db,
        "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'archive' AND c.relname LIKE '%pre_cutover%'",
    ) == (0,)
    assert fetch_one(world_db, "SELECT sentinel FROM archive.audit_export_probe") == (
        37,
    )
    assert apply_pending(world_db, MIGRATIONS_DIR).applied == []
