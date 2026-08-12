"""Migration runner (plan §0.2) — ledger, ordered apply, integrity, postconditions.

Every test runs against a real PostgreSQL scratch database: the subjects under
test are transactions, constraints, and advisory locks, which mocks cannot
exercise. Scratch databases are named runner_test_* and dropped on teardown —
never the shared storyline_test database.
"""

import hashlib
import threading

import psycopg2
import pytest

from scripts.migration_runner import (
    RUNNER_LOCK_KEY,
    MigrationRunnerError,
    apply_pending,
    discover_migrations,
)
from src.config.settings import settings
from tests.scripts.conftest import (
    fetch_ledger,
    table_exists,
    write_migration,
)

pytestmark = pytest.mark.integration


class TestLedgerBootstrap:
    def test_apply_on_empty_dir_creates_ledger_in_runner_schema(
        self, scratch_db, tmp_path
    ):
        report = apply_pending(scratch_db, tmp_path)

        assert report.applied == []
        with psycopg2.connect(scratch_db) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT table_schema FROM information_schema.tables"
                " WHERE table_name = 'schema_migrations'"
            )
            homes = [r[0] for r in cur.fetchall()]
        assert homes == ["runner"], (
            "ledger must live in the dedicated runner schema, never public"
            f" — found in: {homes}"
        )

    def test_ledger_status_check_constraint(self, scratch_db, tmp_path):
        apply_pending(scratch_db, tmp_path)
        with psycopg2.connect(scratch_db) as conn, conn.cursor() as cur:
            with pytest.raises(psycopg2.errors.CheckViolation):
                cur.execute(
                    "INSERT INTO runner.schema_migrations"
                    " (version, checksum, applied_by, status)"
                    " VALUES (999, 'x', 'test', 'bogus')"
                )


class TestApply:
    def test_applies_pending_in_order_and_records(self, scratch_db, tmp_path):
        write_migration(tmp_path, 1, "CREATE TABLE t_one (id INT);")
        write_migration(tmp_path, 2, "CREATE TABLE t_two (ref INT);")

        report = apply_pending(scratch_db, tmp_path)

        assert [m.version for m in report.applied] == [1, 2]
        assert table_exists(scratch_db, "t_one")
        assert table_exists(scratch_db, "t_two")
        rows = fetch_ledger(scratch_db)
        assert [(r[0], r[4]) for r in rows] == [(1, "applied"), (2, "applied")]
        for r in rows:
            assert r[2] == settings.DB_USER  # applied_by = connection user
            assert r[3] is not None and r[3] >= 0  # execution_ms recorded

    def test_checksum_is_sha256_of_file_bytes(self, scratch_db, tmp_path):
        path = write_migration(tmp_path, 1, "CREATE TABLE t_sum (id INT);")

        apply_pending(scratch_db, tmp_path)

        expected = hashlib.sha256(path.read_bytes()).hexdigest()
        assert fetch_ledger(scratch_db)[0][1] == expected

    def test_rerun_is_noop(self, scratch_db, tmp_path):
        write_migration(tmp_path, 1, "CREATE TABLE t_once (id INT);")
        apply_pending(scratch_db, tmp_path)

        report = apply_pending(scratch_db, tmp_path)

        assert report.applied == []
        assert len(fetch_ledger(scratch_db)) == 1

    def test_failure_rolls_back_whole_file_and_stops(self, scratch_db, tmp_path):
        write_migration(tmp_path, 1, "CREATE TABLE t_ok (id INT);")
        write_migration(
            tmp_path,
            2,
            "CREATE TABLE t_partial (id INT);\nSELECT 1/0;",
            name="explodes",
        )
        write_migration(tmp_path, 3, "CREATE TABLE t_after (id INT);")

        with pytest.raises(MigrationRunnerError, match="002"):
            apply_pending(scratch_db, tmp_path)

        assert table_exists(scratch_db, "t_ok")
        assert not table_exists(scratch_db, "t_partial"), (
            "failed migration must roll back atomically — one migration,"
            " one transaction"
        )
        assert not table_exists(scratch_db, "t_after"), (
            "runner must stop at the first failure"
        )
        assert [r[0] for r in fetch_ledger(scratch_db)] == [1]

    def test_ledger_row_and_ddl_are_atomic(self, scratch_db, tmp_path):
        """The ledger insert rides the migration's own transaction: a failure
        after DDL leaves neither the DDL nor the row."""
        write_migration(
            tmp_path,
            1,
            "CREATE TABLE t_atomic (id INT);\nSELECT 1/0;",
        )
        with pytest.raises(MigrationRunnerError):
            apply_pending(scratch_db, tmp_path)
        assert fetch_ledger(scratch_db) == []
        assert not table_exists(scratch_db, "t_atomic")


class TestDiscovery:
    def test_orders_numerically_and_ignores_non_migrations(self, tmp_path):
        write_migration(tmp_path, 2, "SELECT 2;")
        write_migration(tmp_path, 10, "SELECT 10;")
        write_migration(tmp_path, 1, "SELECT 1;")
        (tmp_path / "NOTE_waitlist_table.md").write_text("not a migration")
        (tmp_path / "helper.sql.bak").write_text("not a migration either")

        migrations = discover_migrations(tmp_path)

        assert [m.version for m in migrations] == [1, 2, 10], (
            "numeric order, not lexical — 10 sorts after 2"
        )

    def test_duplicate_version_hard_fails(self, tmp_path):
        write_migration(tmp_path, 7, "SELECT 1;", name="a")
        write_migration(tmp_path, 7, "SELECT 2;", name="b")

        with pytest.raises(MigrationRunnerError, match="007"):
            discover_migrations(tmp_path)


class TestIntegrity:
    def test_edited_applied_file_hard_fails_naming_version(self, scratch_db, tmp_path):
        path = write_migration(tmp_path, 1, "CREATE TABLE t_edit (id INT);")
        apply_pending(scratch_db, tmp_path)

        path.write_text("CREATE TABLE t_edit (id INT); -- edited after apply")

        with pytest.raises(MigrationRunnerError, match="001") as excinfo:
            apply_pending(scratch_db, tmp_path)
        assert "checksum" in str(excinfo.value).lower()

    def test_missing_applied_file_hard_fails(self, scratch_db, tmp_path):
        path = write_migration(tmp_path, 1, "CREATE TABLE t_gone (id INT);")
        apply_pending(scratch_db, tmp_path)

        path.unlink()

        with pytest.raises(MigrationRunnerError, match="001"):
            apply_pending(scratch_db, tmp_path)

    def test_new_file_below_applied_head_hard_fails(self, scratch_db, tmp_path):
        write_migration(tmp_path, 1, "CREATE TABLE t_a (id INT);")
        write_migration(tmp_path, 3, "CREATE TABLE t_c (id INT);")
        apply_pending(scratch_db, tmp_path)

        write_migration(tmp_path, 2, "CREATE TABLE t_b (id INT);")

        with pytest.raises(MigrationRunnerError, match="002"):
            apply_pending(scratch_db, tmp_path)


class TestPostconditions:
    def test_false_postcondition_fails_and_rolls_back(self, scratch_db, tmp_path):
        write_migration(
            tmp_path,
            1,
            "-- runner:postcondition SELECT EXISTS (SELECT 1 FROM"
            " information_schema.tables WHERE table_name = 'nope')\n"
            "CREATE TABLE t_pc (id INT);",
        )

        with pytest.raises(MigrationRunnerError, match="postcondition"):
            apply_pending(scratch_db, tmp_path)

        assert not table_exists(scratch_db, "t_pc")
        assert fetch_ledger(scratch_db) == []

    def test_true_postcondition_passes(self, scratch_db, tmp_path):
        write_migration(
            tmp_path,
            1,
            "-- runner:postcondition SELECT EXISTS (SELECT 1 FROM"
            " information_schema.tables WHERE table_name = 't_pc_ok')\n"
            "CREATE TABLE t_pc_ok (id INT);",
        )

        report = apply_pending(scratch_db, tmp_path)

        assert [m.version for m in report.applied] == [1]


class TestNoTransactionMarker:
    def test_concurrent_index_applies_outside_transaction(self, scratch_db, tmp_path):
        """CREATE INDEX CONCURRENTLY cannot run inside a transaction block —
        this passing proves the marker file executes outside one."""
        write_migration(tmp_path, 1, "CREATE TABLE t_idx (val INT);")
        write_migration(
            tmp_path,
            2,
            "-- runner:no-transaction\n"
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_t_idx_val"
            " ON t_idx (val);",
            name="concurrent_index",
        )

        report = apply_pending(scratch_db, tmp_path)

        assert [m.version for m in report.applied] == [1, 2]
        with psycopg2.connect(scratch_db) as conn, conn.cursor() as cur:
            cur.execute("SELECT indexname FROM pg_indexes WHERE tablename = 't_idx'")
            assert ("ix_t_idx_val",) in cur.fetchall()
        assert [r[0] for r in fetch_ledger(scratch_db)] == [1, 2]


class TestSelfManagedTransactions:
    def test_file_with_own_begin_commit_and_post_commit_concurrently(
        self, scratch_db, tmp_path
    ):
        """The legacy-corpus shape (023): the file manages its own
        transaction and follows it with CREATE INDEX CONCURRENTLY. The
        runner must execute it with psql semantics — statement by statement —
        not wrap it in a second transaction."""
        write_migration(
            tmp_path,
            1,
            "-- Migration: header comment before the transaction\n"
            "BEGIN;\n"
            "CREATE TABLE t_self (val INT);\n"
            "COMMIT;\n"
            "CREATE INDEX CONCURRENTLY ix_t_self_val ON t_self (val);",
            name="self_managed",
        )

        report = apply_pending(scratch_db, tmp_path)

        assert [m.version for m in report.applied] == [1]
        assert table_exists(scratch_db, "t_self")
        with psycopg2.connect(scratch_db) as conn, conn.cursor() as cur:
            cur.execute("SELECT indexname FROM pg_indexes WHERE tablename = 't_self'")
            assert ("ix_t_self_val",) in cur.fetchall()

    def test_self_managed_rollback_still_rolls_back(self, scratch_db, tmp_path):
        """A failure inside the file's own BEGIN block rolls back that
        block's work, and no ledger row is written."""
        write_migration(
            tmp_path,
            1,
            "BEGIN;\nCREATE TABLE t_self_rb (val INT);\nSELECT 1/0;\nCOMMIT;",
            name="self_managed_fail",
        )

        with pytest.raises(MigrationRunnerError, match="001"):
            apply_pending(scratch_db, tmp_path)

        assert not table_exists(scratch_db, "t_self_rb")
        assert fetch_ledger(scratch_db) == []


class TestAdvisoryLock:
    def test_concurrent_runs_serialize_and_apply_once(self, scratch_db, tmp_path):
        """A second runner blocks while the advisory lock is held elsewhere,
        then proceeds and applies exactly once. Deterministic: the lock is
        held by a control connection, not won by sleep timing."""
        write_migration(
            tmp_path,
            1,
            "CREATE TABLE t_race (id SERIAL PRIMARY KEY);\n"
            "INSERT INTO t_race DEFAULT VALUES;",
        )

        holder = psycopg2.connect(scratch_db)
        holder.autocommit = True
        with holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (RUNNER_LOCK_KEY,))

        errors = []

        def run():
            try:
                apply_pending(scratch_db, tmp_path)
            except Exception as exc:  # noqa: BLE001 - collected for assertion
                errors.append(exc)

        runner = threading.Thread(target=run)
        runner.start()
        runner.join(timeout=1.0)
        assert runner.is_alive(), (
            "the runner must block while another invocation holds the lock"
        )

        with holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (RUNNER_LOCK_KEY,))
        holder.close()
        runner.join(timeout=30)

        assert not runner.is_alive() and errors == []
        with psycopg2.connect(scratch_db) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM t_race")
            assert cur.fetchone()[0] == 1
        conn.close()
        assert len(fetch_ledger(scratch_db)) == 1
