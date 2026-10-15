"""The runner grants its own ledger to the API role.

`storydump posture` reads `runner.schema_migrations` as `svc_ingress`.
The runner grants access when it creates or opens the ledger and tolerates
databases where the API role does not exist.
"""

from __future__ import annotations

import psycopg2
import pytest

from scripts.migration_runner import MIGRATIONS_DIR, apply_pending

pytestmark = pytest.mark.integration


def _one(dsn: str, sql: str):
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()


class TestTheLedgerGrant:
    def test_the_api_role_may_read_the_ledger_once_it_exists(self, world_db):
        apply_pending(world_db, MIGRATIONS_DIR)
        (usage, select) = _one(
            world_db,
            "SELECT has_schema_privilege('svc_ingress', 'runner', 'USAGE'),"
            " has_table_privilege('svc_ingress', 'runner.schema_migrations', 'SELECT')",
        )
        assert usage and select, (usage, select)
        (insert,) = _one(
            world_db,
            "SELECT has_table_privilege('svc_ingress', 'runner.schema_migrations',"
            " 'INSERT')",
        )
        assert not insert, "read only: the ledger is the runner's to write"

    def test_a_second_run_is_idempotent(self, world_db):
        apply_pending(world_db, MIGRATIONS_DIR)
        apply_pending(world_db, MIGRATIONS_DIR)
        (select,) = _one(
            world_db,
            "SELECT has_table_privilege('svc_ingress', 'runner.schema_migrations',"
            " 'SELECT')",
        )
        assert select

    def test_a_database_without_the_role_still_gets_its_ledger(
        self, scratch_db, tmp_path
    ):
        report = apply_pending(scratch_db, tmp_path)
        assert report.applied == []
        (present,) = _one(
            scratch_db, "SELECT to_regclass('runner.schema_migrations') IS NOT NULL"
        )
        assert present
