"""``runner repair`` and ``runner status`` (plan §0.2)."""

import psycopg2
import pytest

from scripts.migration_runner import (
    MigrationRunnerError,
    apply_pending,
    repair,
    status,
)
from tests.scripts.conftest import fetch_ledger, write_migration

pytestmark = pytest.mark.integration


class TestRepair:
    def test_repair_records_the_exception_and_unblocks_apply(
        self, scratch_db, tmp_path
    ):
        path = write_migration(tmp_path, 1, "CREATE TABLE t_r (id INT);")
        apply_pending(scratch_db, tmp_path)
        path.write_text("CREATE TABLE t_r (id INT); -- annotated later")
        with pytest.raises(MigrationRunnerError, match="001"):
            apply_pending(scratch_db, tmp_path)

        repair(scratch_db, tmp_path, version=1, reason="annotation only")

        rows = fetch_ledger(scratch_db)
        assert rows[0][4] == "repaired"
        report = apply_pending(scratch_db, tmp_path)  # integrity clean again
        assert report.applied == []

    def test_repair_unknown_version_fails(self, scratch_db, tmp_path):
        write_migration(tmp_path, 1, "CREATE TABLE t_r2 (id INT);")
        apply_pending(scratch_db, tmp_path)

        with pytest.raises(MigrationRunnerError, match="007"):
            repair(scratch_db, tmp_path, version=7, reason="nope")

    def test_repair_requires_a_reason(self, scratch_db, tmp_path):
        write_migration(tmp_path, 1, "CREATE TABLE t_r3 (id INT);")
        apply_pending(scratch_db, tmp_path)

        with pytest.raises(MigrationRunnerError, match="reason"):
            repair(scratch_db, tmp_path, version=1, reason="   ")


class TestStatus:
    def test_status_on_virgin_database_creates_nothing(self, scratch_db, tmp_path):
        write_migration(tmp_path, 1, "CREATE TABLE t_s (id INT);")

        report = status(scratch_db, tmp_path)

        assert report.ledger_present is False
        assert [m.version for m in report.pending] == [1]
        with psycopg2.connect(scratch_db) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT EXISTS (SELECT 1 FROM information_schema.schemata"
                " WHERE schema_name = 'runner')"
            )
            assert cur.fetchone()[0] is False, (
                "status is a read door — it must not create the ledger"
            )

    def test_status_reports_applied_pending_and_discrepancies(
        self, scratch_db, tmp_path
    ):
        p1 = write_migration(tmp_path, 1, "CREATE TABLE t_s1 (id INT);")
        apply_pending(scratch_db, tmp_path)
        write_migration(tmp_path, 2, "CREATE TABLE t_s2 (id INT);")
        p1.write_text("CREATE TABLE t_s1 (id INT); -- drifted")

        report = status(scratch_db, tmp_path)

        assert report.ledger_present is True
        assert [v for v, _ in report.discrepancies] == [1]
        assert [m.version for m in report.pending] == [2]


class TestExecutionMode:
    def test_status_and_discovery_expose_execution_mode(self, scratch_db, tmp_path):
        """How a file will run is a declared discovery-time fact, not an
        apply-time sniff — status can show it and reviewers can see it."""
        write_migration(tmp_path, 1, "CREATE TABLE em_w (id INT);")
        write_migration(
            tmp_path, 2, "BEGIN;\nCREATE TABLE em_s (id INT);\nCOMMIT;", name="s"
        )
        write_migration(tmp_path, 3, "-- runner:no-transaction\nSELECT 1;", name="n")

        from scripts.migration_runner import discover_migrations

        modes = {m.version: m.execution_mode for m in discover_migrations(tmp_path)}
        assert modes == {1: "wrapped", 2: "self-managed", 3: "no-transaction"}

        report = status(scratch_db, tmp_path)
        assert [m.execution_mode for m in report.pending] == [
            "wrapped",
            "self-managed",
            "no-transaction",
        ]
