"""The runner grants its own ledger to the API role (the v2 CLI plan, F7 (c)).

`storydump posture` reads `runner.schema_migrations` as `svc_ingress`. The
grant cannot live in the advertised stream — the gates replay that stream
into a database with no `runner` schema — so the runner makes it where it
makes the ledger, on every run, guarded on the role existing (the first
ledger is created before 057 creates the roles).
"""

from __future__ import annotations

import psycopg2
import pytest

from scripts.migration_runner import apply_pending

pytestmark = pytest.mark.integration


def _one(dsn: str, sql: str):
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()


class TestTheLedgerGrant:
    def test_the_api_role_may_read_the_ledger_once_it_exists(
        self, bootstrapped_db, tmp_path
    ):
        apply_pending(bootstrapped_db, tmp_path)
        (usage, select) = _one(
            bootstrapped_db,
            "SELECT has_schema_privilege('svc_ingress', 'runner', 'USAGE'),"
            " has_table_privilege('svc_ingress', 'runner.schema_migrations', 'SELECT')",
        )
        assert usage and select, (usage, select)
        (insert,) = _one(
            bootstrapped_db,
            "SELECT has_table_privilege('svc_ingress', 'runner.schema_migrations',"
            " 'INSERT')",
        )
        assert not insert, "read only: the ledger is the runner's to write"

    def test_a_second_run_is_idempotent(self, bootstrapped_db, tmp_path):
        apply_pending(bootstrapped_db, tmp_path)
        apply_pending(bootstrapped_db, tmp_path)
        (select,) = _one(
            bootstrapped_db,
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
