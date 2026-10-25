"""Migration runner — ledger, ordered apply, integrity, postconditions.

Every test runs against a real PostgreSQL scratch database: the subjects under
test are transactions, constraints, and advisory locks, which mocks cannot
exercise. Scratch databases are named runner_test_* and dropped on teardown —
never the shared storyline_test database.
"""

import hashlib
import json
import threading
import time
from types import SimpleNamespace

import psycopg2
from psycopg2.extensions import make_dsn, parse_dsn
import pytest

import scripts.migration_runner as runner_module
from scripts.migration_runner import (
    INITIAL_VERSION,
    RUNNER_LOCK_KEY,
    MigrationRunnerError,
    _connect,
    adopt,
    apply_manual,
    apply_pending,
    discover_migrations,
    main,
    status,
)
from src.config.db_url import userinfo
from src.config.settings import settings
from tests.scripts.conftest import (
    fetch_ledger,
    fetch_one,
    table_exists,
    txn,
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


class TestMarkers:
    """`runner:` lines are the file's contract with the runner, so a marker the
    runner does not know is a hard failure at discovery — a misspelt
    `runner:manaul` would otherwise be an ordinary file applied at the next
    deploy."""

    def test_an_unknown_marker_is_refused_at_discovery_naming_the_file(self, tmp_path):
        write_migration(tmp_path, 1, "-- runner:manaul\nCREATE TABLE t_a (id INT);")
        with pytest.raises(MigrationRunnerError, match=r"001.*runner:manaul"):
            discover_migrations(tmp_path)

    @pytest.mark.parametrize(
        "line",
        [
            "-- runner: gated",  # a space after the colon
            "-- Runner:gated",  # a capital R
            "--runner:gated",  # no space after the dashes
            "-- runner:MANUAL",  # a capital word: the words are exact
            "-- runner:reapply-safe because it re-runs",  # a flag with an argument
        ],
    )
    def test_every_spelling_that_reads_as_a_marker_reaches_the_known_set(
        self, tmp_path, line
    ):
        """An UNKNOWN word (`gated`, `MANUAL`) in every frame the regex
        tolerates is refused at the known-set check — the frame is lenient,
        the word is exact."""
        write_migration(tmp_path, 1, f"{line}\nCREATE TABLE t_a (id INT);")
        with pytest.raises(MigrationRunnerError, match="001"):
            discover_migrations(tmp_path)

    @pytest.mark.parametrize(
        "line", ["-- runner: manual", "-- Runner:manual", "--runner:manual"]
    )
    def test_the_frame_is_lenient_where_the_word_is_known(self, tmp_path, line):
        """The twin: the same slipped frames around a KNOWN word are read as
        that marker, never as prose — a `manual` file with a stray space is
        still gated."""
        write_migration(tmp_path, 1, f"{line}\nDROP TABLE t_gone;")
        [m] = discover_migrations(tmp_path)
        assert m.manual is True

    def test_a_bare_postcondition_marker_is_refused(self, tmp_path):
        write_migration(
            tmp_path, 1, "-- runner:postcondition\nCREATE TABLE t_a (id INT);"
        )
        with pytest.raises(MigrationRunnerError, match="bare marker"):
            discover_migrations(tmp_path)

    def test_prose_that_merely_mentions_a_marker_is_not_one(self, tmp_path):
        write_migration(
            tmp_path,
            1,
            "-- The runner:manual marker is what keeps a file out of the deploy.\n"
            "-- see runner:postcondition lines below\n"
            "CREATE TABLE t_a (id INT);",
        )
        [m] = discover_migrations(tmp_path)
        assert m.postconditions == ()

    def test_a_known_marker_with_a_typo_in_its_argument_is_still_a_postcondition(
        self, tmp_path
    ):
        """The postcondition marker carries SQL after it; the refusal is on the
        marker word, never on what follows it."""
        write_migration(
            tmp_path,
            1,
            "-- runner:postcondition SELECT tru\nCREATE TABLE t_a (id INT);",
        )
        [m] = discover_migrations(tmp_path)
        assert m.postconditions == ("SELECT tru",)

    @pytest.mark.parametrize(
        "line",
        [
            "--- runner:manual",  # three dashes
            "-- -- runner:manual",  # an editor's "comment this line" on a comment
            "/* runner:manual */",  # a block-comment frame
            "# runner:manual",  # a shell-style frame
            "-- runner manual",  # no colon
            "DROP TABLE t_gone; -- runner:manual",  # after code on the same line
            "-- RUNNER reapply-safe",  # another word, upper case, no colon
            "-- runner-manual",  # a dash for the colon
            "-- runner=manual",  # an equals sign for the colon
        ],
    )
    def test_a_near_miss_is_refused_never_read_as_prose(self, tmp_path, line):
        """Each of these, read as prose, would make a `manual` file an ordinary
        one the next predeploy applies — the whole hazard. The frame the grammar
        reads is `-- runner:<word>` alone at the start of its line; anything
        that LOOKS like an attempt and is not that is refused."""
        write_migration(tmp_path, 1, f"{line}\nCREATE TABLE t_a (id INT);")
        with pytest.raises(MigrationRunnerError, match="001") as exc:
            discover_migrations(tmp_path)
        assert "near miss" in str(exc.value)

    def test_a_mention_of_a_marker_in_a_sentence_is_still_prose(self, tmp_path):
        """The near-miss rule keys on the opener being followed by `runner`,
        so prose that names a marker mid-sentence stays prose."""
        write_migration(
            tmp_path,
            1,
            "-- The runner:manual marker is what keeps a file out of the deploy.\n"
            "-- python -m scripts.migration_runner apply --manual 5 is the door.\n"
            "CREATE TABLE t_a (id INT);",
        )
        [m] = discover_migrations(tmp_path)
        assert m.manual is False

    def test_a_byte_order_mark_does_not_hide_a_marker_on_line_one(self, tmp_path):
        path = tmp_path / "001_bom.sql"
        path.write_bytes("\ufeff-- runner:manual\nDROP TABLE t_gone;".encode("utf-8"))
        [m] = discover_migrations(tmp_path)
        assert m.manual is True

    def test_a_file_the_runner_cannot_decode_is_refused_naming_it(self, tmp_path):
        path = tmp_path / "001_utf16.sql"
        path.write_bytes("-- runner:manual\nDROP TABLE t_gone;".encode("utf-16"))
        with pytest.raises(MigrationRunnerError, match="001") as exc:
            discover_migrations(tmp_path)
        assert "not UTF-8" in str(exc.value)

    def test_a_file_with_nul_bytes_is_refused_naming_it(self, tmp_path):
        """UTF-16 WITHOUT a byte-order mark is valid UTF-8 — ASCII with a NUL
        after every character — so the decode passes and every marker in it
        reads as prose. Refused at discovery, by name, like the undecodable
        file; without this, psycopg2's NUL refusal at apply is the only guard."""
        path = tmp_path / "001_utf16le.sql"
        path.write_bytes("-- runner:manual\nDROP TABLE t_gone;".encode("utf-16-le"))
        with pytest.raises(MigrationRunnerError, match="001") as exc:
            discover_migrations(tmp_path)
        assert "NUL" in str(exc.value)

    def test_manual_is_read_off_the_file(self, tmp_path):
        write_migration(tmp_path, 1, "-- runner:manual\nDROP TABLE t_gone;")
        write_migration(tmp_path, 2, "CREATE TABLE t_b (id INT);")
        a, b = discover_migrations(tmp_path)
        assert a.manual is True
        assert b.manual is False

    def test_a_manual_marker_refuses_an_argument(self, tmp_path):
        write_migration(tmp_path, 1, "-- runner:manual 079\nDROP TABLE t_gone;")
        with pytest.raises(MigrationRunnerError, match="001"):
            discover_migrations(tmp_path)


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
        """The legacy-corpus shape (account-scoped settings): the file manages its own
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

    def test_a_lock_held_past_the_bound_fails_the_run_and_names_its_holder(
        self, scratch_db, tmp_path, monkeypatch, capsys
    ):
        """A lock nobody lets go must fail the run, not hang the deploy. A
        control session keeps the lock, and the bound (seconds) is shortened to
        fit the test. `raising=False`, so that on a runner without a bound this
        fails on the test's own clock rather than at setup."""
        monkeypatch.setattr(runner_module, "RUNNER_LOCK_WAIT", 0.3, raising=False)
        write_migration(tmp_path, 1, "CREATE TABLE t_waited (id INT);")
        holder = psycopg2.connect(scratch_db)
        holder.autocommit = True
        with holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (RUNNER_LOCK_KEY,))
        holder_pid = holder.get_backend_pid()
        thread, errors = _apply_in_thread(scratch_db, tmp_path)
        try:
            thread.join(timeout=10)
            assert not thread.is_alive(), (
                "the run is still waiting on a lock nobody lets go: a deploy's"
                " predeploy would hang on it"
            )
        finally:
            holder.close()
            thread.join(timeout=30)
        assert len(errors) == 1 and isinstance(errors[0], MigrationRunnerError)
        assert "runner lock was still held" in str(errors[0])
        assert f"pid {holder_pid}" in str(errors[0]), "the error must name the holder"
        assert "waiting up to 0.3s" in capsys.readouterr().err
        assert not table_exists(scratch_db, "t_waited"), "nothing may be applied"

    def test_a_waiting_run_lets_the_holder_build_an_index_concurrently(
        self, scratch_db, tmp_path, capsys
    ):
        """Run A holds the lock and builds an index CONCURRENTLY while run B
        waits for it. The build waits for every older snapshot, so a wait that
        held one (a blocking `pg_advisory_lock` statement) deadlocks with it:
        the build fails and leaves an INVALID index, or the waiter dies. B must
        wait holding nothing, then take the lock and find nothing pending."""
        write_migration(
            tmp_path,
            1,
            "CREATE TABLE t_ix (v INT);\nINSERT INTO t_ix SELECT generate_series(1, 100);",
        )
        apply_pending(scratch_db, tmp_path)
        # The sleep keeps A applying, lock held, until B is waiting; then A
        # builds the index while B still waits.
        write_migration(
            tmp_path,
            2,
            "-- runner:no-transaction\n"
            "SELECT pg_sleep(2);\n"
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_t_ix_v ON t_ix (v);",
            name="index_concurrently",
        )
        results = {}

        def run(name):
            try:
                results[name] = apply_pending(scratch_db, tmp_path)
            except Exception as exc:  # noqa: BLE001 - collected for assertion
                results[name] = exc

        first = threading.Thread(target=run, args=("A",))
        first.start()
        poll = psycopg2.connect(scratch_db)
        poll.autocommit = True
        try:
            with poll.cursor() as cur:
                assert _until(lambda: _holder_sleeps(cur), 10), (
                    "run A never took the lock and began applying"
                )
            second = threading.Thread(target=run, args=("B",))
            second.start()
            first.join(timeout=60)
            second.join(timeout=60)
        finally:
            poll.close()
        assert not first.is_alive() and not second.is_alive()
        assert not isinstance(results["A"], Exception), (
            f"the holder's run failed while another run waited: {results['A']}"
        )
        assert not isinstance(results["B"], Exception), (
            f"the waiting run failed: {results['B']}"
        )
        assert fetch_one(
            scratch_db,
            "SELECT indisvalid FROM pg_index WHERE indexrelid = 'ix_t_ix_v'::regclass",
        ) == (True,), "the concurrent build left an INVALID index"
        assert [m.version for m in results["A"].applied] == [2]
        assert results["B"].applied == [], "the waiting run must find nothing pending"
        assert "the runner lock is held by" in capsys.readouterr().err, (
            "run B never waited, so the test proved nothing"
        )
        assert [r[0] for r in fetch_ledger(scratch_db)] == [1, 2]

    def test_a_run_gives_its_lock_back_before_it_closes(
        self, scratch_db, tmp_path, monkeypatch
    ):
        """Closing frees the lock only where the connection is the session.
        Here close() keeps the session open, as a pooler does, and the lock
        must be free once the run returns all the same."""
        sessions = []
        monkeypatch.setattr(
            runner_module, "_connect", _connect_keeping_sessions(sessions)
        )
        write_migration(tmp_path, 1, "CREATE TABLE t_given_back (id INT);")
        try:
            apply_pending(scratch_db, tmp_path)
            assert _lock_is_free(scratch_db), (
                "the run left its lock on a session that outlived it: the next"
                " deploy's runner would wait on it"
            )
        finally:
            for conn in sessions:
                conn.close()

    def test_a_failed_self_managed_file_still_gives_the_lock_back(
        self, scratch_db, tmp_path, monkeypatch
    ):
        """A file with its own BEGIN that fails leaves its block open on the
        session, and the server refuses an unlock inside an aborted block.
        The release ends the block first."""
        sessions = []
        monkeypatch.setattr(
            runner_module, "_connect", _connect_keeping_sessions(sessions)
        )
        write_migration(
            tmp_path,
            1,
            "BEGIN;\nCREATE TABLE t_open_block (v INT);\nSELECT 1/0;\nCOMMIT;",
            name="self_managed_fail",
        )
        try:
            with pytest.raises(MigrationRunnerError, match="001"):
                apply_pending(scratch_db, tmp_path)
            assert _lock_is_free(scratch_db)
        finally:
            for conn in sessions:
                conn.close()

    def test_a_self_managed_file_left_open_fails_the_run(
        self, scratch_db, tmp_path, capsys
    ):
        """A file with its own BEGIN and no COMMIT applies inside a block that
        nothing commits, and the release's rollback then discards it. The run
        must fail and say so, not report the file applied and exit 0."""
        write_migration(
            tmp_path, 1, "BEGIN;\nCREATE TABLE t_left_open (v INT);", name="no_commit"
        )
        code = main(
            ["--database-url", scratch_db, "--migrations-dir", str(tmp_path), "apply"]
        )
        out, err = capsys.readouterr()
        assert code == 1, f"the run reported success: {out!r}"
        assert "a self-managed file left its transaction open" in err
        assert (
            "the open transaction was rolled back and the file is not recorded" in err
        )
        assert "applied 001" not in out
        assert not table_exists(scratch_db, "t_left_open")
        assert fetch_ledger(scratch_db) == [], "the file must not be recorded"
        assert _lock_is_free(scratch_db)

    def test_a_no_transaction_file_left_open_is_named_for_its_mode(
        self, scratch_db, tmp_path, capsys
    ):
        """A `-- runner:no-transaction` file commits statement by statement, so
        one that ends inside a BEGIN has committed what came before it: the
        refusal names the file's own mode, and only the open transaction is
        rolled back."""
        write_migration(
            tmp_path,
            1,
            "-- runner:no-transaction\n"
            "CREATE TABLE t_committed_first (v INT);\n"
            "BEGIN;\nCREATE TABLE t_left_in_block (v INT);",
            name="trailing_begin",
        )
        code = main(
            ["--database-url", scratch_db, "--migrations-dir", str(tmp_path), "apply"]
        )
        out, err = capsys.readouterr()
        assert code == 1, f"the run reported success: {out!r}"
        assert "a no-transaction file left its transaction open" in err
        assert (
            "the open transaction was rolled back and the file is not recorded" in err
        )
        assert table_exists(scratch_db, "t_committed_first"), (
            "a statement before the BEGIN commits on its own"
        )
        assert not table_exists(scratch_db, "t_left_in_block")
        assert fetch_ledger(scratch_db) == []
        assert _lock_is_free(scratch_db)

    def test_a_self_managed_file_left_open_fails_before_the_next_file_runs(
        self, scratch_db, tmp_path, capsys
    ):
        """A wrapped file after an unclosed self-managed one starts its
        transaction inside the open block, and its COMMIT commits both. So the
        run must fail at the file that left the block open, naming it, before
        anything after it runs."""
        write_migration(
            tmp_path,
            1,
            "BEGIN;\nCREATE TABLE t_left_open_first (v INT);",
            name="no_commit",
        )
        write_migration(
            tmp_path, 2, "CREATE TABLE t_after_open (v INT);", name="wrapped"
        )
        code = main(
            ["--database-url", scratch_db, "--migrations-dir", str(tmp_path), "apply"]
        )
        out, err = capsys.readouterr()
        assert code == 1, f"the run reported success: {out!r}"
        assert "migration 001" in err and "left its transaction open" in err
        assert "applied" not in out
        assert not table_exists(scratch_db, "t_left_open_first")
        assert not table_exists(scratch_db, "t_after_open"), "the next file ran"
        assert fetch_ledger(scratch_db) == []
        assert _lock_is_free(scratch_db)

    def test_a_verb_that_returns_inside_a_transaction_fails_the_run(self, scratch_db):
        """The backstop behind the per-file check: a verb that returns with its
        connection still inside a transaction fails, rather than have the
        release's rollback discard that transaction unseen."""
        with pytest.raises(MigrationRunnerError, match="left a transaction open"):
            with runner_module._locked_connection(scratch_db) as conn:
                with conn.cursor() as cur:
                    cur.execute("BEGIN")
                    cur.execute("CREATE TABLE t_backstop (v INT)")
        assert not table_exists(scratch_db, "t_backstop")
        assert _lock_is_free(scratch_db)

    def test_a_run_whose_session_lost_its_lock_says_so(
        self, scratch_db, tmp_path, capsys
    ):
        """The runner's own witness for a pooler its host name does not show:
        a run whose session no longer holds its lock at the end ran on more
        than one server session. A file that drops every session lock stands
        in for that here."""
        write_migration(tmp_path, 1, "SELECT pg_advisory_unlock_all();")
        apply_pending(scratch_db, tmp_path)
        assert "was not held by this session" in capsys.readouterr().err

    def test_a_dead_connection_at_the_end_does_not_hide_the_runs_error(
        self, scratch_db, tmp_path, capsys
    ):
        """The unlock runs in a `finally`. It must never raise there, or its
        error would replace the run's own."""
        write_migration(tmp_path, 1, "SELECT pg_terminate_backend(pg_backend_pid());")
        with pytest.raises(MigrationRunnerError, match="001"):
            apply_pending(scratch_db, tmp_path)
        assert "not given back explicitly" in capsys.readouterr().err


class TestTheRefusalReadsLibpqsHost:
    """The runner judges `conn.info.host`, the host libpq connected to, so a
    connection string in any form libpq accepts is classified the way libpq
    resolved it. No pooler is reachable from a test, so the predicate is
    replaced by one that refuses whatever host it is shown, and records it."""

    @pytest.mark.parametrize(
        "form", ["host_parameter", "key_value", "host_list", "pghost"]
    )
    def test_the_host_judged_is_the_one_libpq_connected_to(
        self, scratch_db, tmp_path, monkeypatch, form
    ):
        parts = parse_dsn(scratch_db)
        host, port = parts["host"], parts.get("port", "5432")
        rest = {k: v for k, v in parts.items() if k not in ("host", "port")}
        if form == "host_parameter":  # `host=` in a URL's query
            dsn = (
                f"postgresql://{userinfo(rest.get('user'), rest.get('password'))}"
                f"/{rest['dbname']}?host={host}&port={port}"
            )
        elif form == "key_value":
            dsn = make_dsn(**parts)
        elif form == "host_list":
            # The first entry refuses the connection (port 1), so libpq
            # connects to the second: a runner that judged the first listed
            # host would be seen judging the decoy.
            decoy = "localhost" if host == "127.0.0.1" else "127.0.0.1"
            dsn = make_dsn(**rest, host=f"{decoy},{host}", port=f"1,{port}")
        else:  # no host in the string: libpq takes PGHOST
            monkeypatch.setenv("PGHOST", host)
            dsn = make_dsn(**rest, port=port)
        seen = []

        def refuse_every_host(shown):
            seen.append(shown)
            return True

        monkeypatch.setattr(
            runner_module, "is_pooled_host", refuse_every_host, raising=False
        )
        with pytest.raises(
            MigrationRunnerError, match="names a pooled database endpoint"
        ):
            apply_pending(dsn, tmp_path)
        assert seen == [host], (
            f"the runner judged {seen}, not the host libpq connected to"
        )
        assert _lock_is_free(scratch_db), "the refusal came after the lock"
        assert not table_exists(scratch_db, "schema_migrations", schema="runner"), (
            "a statement ran before the refusal"
        )


class TestAPooledEndpointIsRefused:
    """A pooled host is refused after libpq connects and before any statement.
    `_connect` hands back a fake connection that says which host libpq
    reached."""

    POOLED_HOST = "ep-xxx-pooler.region.aws.neon.tech"
    DIRECT_HOST = "ep-xxx.region.aws.neon.tech"
    PASSWORD = "not-a-real-password"

    def _main(self, tmp_path, host):
        return main(
            [
                "--database-url",
                f"postgresql://owner:{self.PASSWORD}@{host}/db?sslmode=require",
                "--migrations-dir",
                str(tmp_path),
                "apply",
            ]
        )

    def test_the_cli_refuses_it_before_any_statement_and_never_prints_the_url(
        self, tmp_path, monkeypatch, capsys
    ):
        fake = _FakeConnection(self.POOLED_HOST)
        monkeypatch.setattr(runner_module, "_connect", lambda dsn: fake)
        code = self._main(tmp_path, self.POOLED_HOST)
        err = capsys.readouterr().err
        assert code == 1
        assert "names a pooled database endpoint" in err and "direct host" in err
        assert self.POOLED_HOST not in err and self.PASSWORD not in err
        assert fake.closed_by_runner, "the refused connection must be closed"

    def test_control_a_direct_host_goes_on_to_take_the_lock(
        self, tmp_path, monkeypatch
    ):
        class Reached(Exception):
            pass

        fake = _FakeConnection(self.DIRECT_HOST, on_cursor=Reached())
        monkeypatch.setattr(runner_module, "_connect", lambda dsn: fake)
        with pytest.raises(Reached):
            self._main(tmp_path, self.DIRECT_HOST)

    def test_every_writing_verb_opens_the_one_guarded_door(self):
        """Only `_locked_connection` takes the lock, only it and the read-only
        `status` call `_connect`, and only `_connect` calls `psycopg2.connect`.
        A verb that connected another way would be neither refused a pooled
        host nor bounded nor unlocked."""
        import ast
        import inspect

        def called(node):
            func = node.func
            if isinstance(func, ast.Name):
                return func.id
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                return f"{func.value.id}.{func.attr}"
            return None

        tree = ast.parse(inspect.getsource(runner_module))
        callers = {"_acquire_lock": set(), "_connect": set(), "psycopg2.connect": set()}
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and called(node) in callers:
                    callers[called(node)].add(fn.name)
        assert callers["_acquire_lock"] == {"_locked_connection"}
        assert callers["_connect"] == {"_locked_connection", "status"}
        assert callers["psycopg2.connect"] == {"_connect"}


class TestAnUnreadableUrlIsRefused:
    """libpq refuses a connection string it cannot parse and quotes the part
    it choked on, which can be the password. The runner names the fix
    instead, one hint for every such cause. No database: libpq refuses before
    it connects."""

    @pytest.mark.parametrize(
        "dsn",
        [
            "postgresql://owner:not-a-real-password-%zz@db.invalid/db",
            "postgresql://owner:not-a-real-password@db.invalid/db?sslmod=require",
            "postgresql://owner:not-a-real-password@db.invalid/db?sslmode",
            "host=db.invalid user=owner password=not-a-real-password bogus=1",
        ],
        ids=[
            "invalid-escape-in-password",
            "mistyped-query-parameter",
            "query-parameter-without-equals",
            "unknown-key-value-option",
        ],
    )
    def test_the_cli_refuses_it_without_printing_the_password(
        self, tmp_path, capsys, dsn
    ):
        code = main(["--database-url", dsn, "--migrations-dir", str(tmp_path), "apply"])
        out, err = capsys.readouterr()
        assert code == 1
        assert "is not one libpq can read" in err
        assert "check each query parameter" in err, (
            "the hint names only the password, and this string's fault is elsewhere"
        )
        assert "not-a-real-password" not in out + err, "the refusal printed it"
        # Nor may the refusal carry libpq's error along with it: a logger or
        # a traceback that walks the chain would print the password.
        with pytest.raises(MigrationRunnerError) as caught:
            _connect(dsn)
        chain = (caught.value.__cause__, caught.value.__context__)
        assert not any("not-a-real-password" in str(link) for link in chain), (
            "the refusal chains libpq's error, which quotes the password"
        )


def _column_exists(dsn, table, column):
    return fetch_one(
        dsn,
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE"
        " table_schema = 'public' AND table_name = %s AND column_name = %s)",
        (table, column),
    )[0]


def _lock_waiters(cur, table) -> int:
    """Lock requests on *table* still queued (not granted)."""
    cur.execute(
        "SELECT count(*) FROM pg_locks l JOIN pg_class c ON c.oid = l.relation"
        " WHERE c.relname = %s AND NOT l.granted",
        (table,),
    )
    return cur.fetchone()[0]


def _until(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _apply_in_thread(dsn, migrations_dir):
    """`apply_pending` on a thread; its exception, if any, lands in `errors`."""
    errors = []

    def run():
        try:
            apply_pending(dsn, migrations_dir)
        except Exception as exc:  # noqa: BLE001 - collected for assertion
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, errors


class _SessionOutlivesClose:
    """A connection whose close() leaves its server session open: the shape a
    connection pooler gives its client, since the pooler keeps the server
    session and lends it on. Every other attribute is the real connection's."""

    def __init__(self, conn):
        object.__setattr__(self, "_conn", conn)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_conn"), name)

    def __setattr__(self, name, value):
        setattr(object.__getattribute__(self, "_conn"), name, value)

    def close(self):
        """Keep the session: the pooler's half."""


def _connect_keeping_sessions(sessions):
    """The runner's own `_connect` (imported before any test patches it),
    with connections that outlive close(); each real one is kept in
    *sessions* for the test to close."""

    def connect(dsn):
        conn = _connect(dsn)
        sessions.append(conn)
        return _SessionOutlivesClose(conn)

    return connect


def _lock_is_free(dsn) -> bool:
    """Whether another session can take the runner lock now."""
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (RUNNER_LOCK_KEY,))
            free = cur.fetchone()[0]
            if free:
                cur.execute("SELECT pg_advisory_unlock(%s)", (RUNNER_LOCK_KEY,))
        return free
    finally:
        conn.close()


def _holder_sleeps(cur) -> bool:
    """Whether the session holding the runner lock in *cur*'s database is
    inside `pg_sleep`: a run that has taken the lock and is applying."""
    cur.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_locks l"
        " JOIN pg_stat_activity a ON a.pid = l.pid"
        " WHERE l.locktype = 'advisory' AND l.granted AND l.objsubid = 1"
        " AND l.database = (SELECT oid FROM pg_database"
        " WHERE datname = current_database())"
        " AND ((l.classid::bigint << 32) | l.objid::bigint) = %s"
        " AND a.wait_event = 'PgSleep')",
        (RUNNER_LOCK_KEY,),
    )
    return cur.fetchone()[0]


class _FakeConnection:
    """What `_locked_connection` sees of a connection libpq opened to *host*:
    its `info.host`. No statement may reach it before the host is judged, so
    a cursor raises *on_cursor*, by default a test failure."""

    def __init__(self, host, on_cursor=None):
        self.info = SimpleNamespace(host=host)
        self.closed_by_runner = False
        self._on_cursor = on_cursor or AssertionError(
            "a statement was sent before the host was refused"
        )

    def cursor(self):
        raise self._on_cursor

    def close(self):
        self.closed_by_runner = True


class TestTheLockWaitIsBounded:
    """A wrapped file's lock waits are bounded (`LOCK_TIMEOUT`), and a
    lock-wait failure, and no other, is tried again (`LOCK_RETRY_DELAYS_S`).

    A holder reads the table in an open transaction (ACCESS SHARE until it
    rolls back) while the runner applies an ALTER that needs ACCESS EXCLUSIVE.
    Each test keeps its own deadline, so a runner whose wait is unbounded
    fails on the test's clock instead of hanging it. The knobs are patched
    with `raising=False`, so on a runner without them the tests fail on the
    unbounded wait itself rather than at setup."""

    def _write_alter(self, scratch_db, tmp_path):
        write_migration(tmp_path, 1, "CREATE TABLE t_held (id INT);")
        apply_pending(scratch_db, tmp_path)
        write_migration(
            tmp_path, 2, "ALTER TABLE t_held ADD COLUMN extra INT;", name="alter"
        )

    def test_a_wrapped_file_runs_under_a_lock_bound(self, scratch_db, tmp_path):
        write_migration(
            tmp_path,
            1,
            "-- runner:postcondition SELECT current_setting('lock_timeout') <> '0'\n"
            "CREATE TABLE t_bounded (id INT);",
        )
        apply_pending(scratch_db, tmp_path)
        assert table_exists(scratch_db, "t_bounded")

    def test_a_held_table_fails_the_file_fast_and_leaves_nothing(
        self, scratch_db, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(runner_module, "LOCK_TIMEOUT", "20ms", raising=False)
        monkeypatch.setattr(
            runner_module, "LOCK_RETRY_DELAYS_S", (0.0, 0.0), raising=False
        )
        self._write_alter(scratch_db, tmp_path)
        with txn(scratch_db) as holder:
            with holder.cursor() as cur:
                cur.execute("SELECT count(*) FROM t_held")
            thread, errors = _apply_in_thread(scratch_db, tmp_path)
            thread.join(timeout=10)
            finished = not thread.is_alive()
        thread.join(timeout=30)  # the holder let go, so even an unbounded runner ends
        assert finished, "the runner waited on the holder: its lock wait is unbounded"
        [error] = errors
        assert isinstance(error, MigrationRunnerError) and "002" in str(error)
        assert isinstance(error.__cause__, psycopg2.errors.LockNotAvailable)
        assert [r[0] for r in fetch_ledger(scratch_db)] == [1], "no ledger row"
        assert not _column_exists(scratch_db, "t_held", "extra"), "no partial change"

    def test_a_holder_that_lets_go_is_outlasted_by_the_retry(
        self, scratch_db, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(runner_module, "LOCK_TIMEOUT", "200ms", raising=False)
        monkeypatch.setattr(runner_module, "LOCK_RETRY_DELAYS_S", (1.0,), raising=False)
        self._write_alter(scratch_db, tmp_path)
        poll = psycopg2.connect(scratch_db)
        poll.autocommit = True
        try:
            with txn(scratch_db) as holder, poll.cursor() as cur:
                with holder.cursor() as held:
                    held.execute("SELECT count(*) FROM t_held")
                thread, errors = _apply_in_thread(scratch_db, tmp_path)
                queued = (
                    _until(
                        lambda: (
                            not thread.is_alive() or _lock_waiters(cur, "t_held") > 0
                        ),
                        10,
                    )
                    and thread.is_alive()
                )
                gave_up = queued and _until(
                    lambda: _lock_waiters(cur, "t_held") == 0, 5
                )
            # Leaving the block rolled the holder back: it let go.
            thread.join(timeout=30)
        finally:
            poll.close()
        assert queued, f"the runner never queued for the table's lock: {errors}"
        assert gave_up, "the first attempt never gave up its place in the queue"
        assert errors == [] and not thread.is_alive()
        assert [r[0] for r in fetch_ledger(scratch_db)] == [1, 2]
        assert _column_exists(scratch_db, "t_held", "extra")

    @pytest.mark.parametrize(
        "failure, marker, attempts",
        [
            (psycopg2.errors.LockNotAvailable, "", 3),
            (psycopg2.errors.DivisionByZero, "", 1),
            # A non-wrapped file's statements commit as they go: never again.
            (psycopg2.errors.LockNotAvailable, "-- runner:no-transaction\n", 1),
        ],
        ids=["lock-wait", "other-failure", "not-wrapped"],
    )
    def test_only_a_wrapped_files_lock_wait_is_tried_again(
        self, tmp_path, monkeypatch, failure, marker, attempts
    ):
        monkeypatch.setattr(
            runner_module, "LOCK_RETRY_DELAYS_S", (0.0, 0.0), raising=False
        )
        write_migration(tmp_path, 1, marker + "CREATE TABLE t_x (id INT);")
        [migration] = discover_migrations(tmp_path)
        calls = []

        def fails(conn, m):
            calls.append(m.version)
            raise failure()

        monkeypatch.setattr(runner_module, "_apply_one", fails)
        with pytest.raises(MigrationRunnerError, match="001") as caught:
            runner_module._apply_guarded(None, migration)
        assert isinstance(caught.value.__cause__, failure)
        assert len(calls) == attempts


class TestManual:
    """`-- runner:manual`: a file the deploy
    must never run by itself. `apply` skips it where it stands and reports
    it as OWED — exit 0, so a deploy is never failed by a file that is
    waiting for an operator — and it is exempt from the below-head rule in
    both doors: `apply` keeps applying ordinary files numbered ABOVE it, and
    `apply --manual <version>` applies it below the head, by name, exactly
    like any file (ledger row, postconditions, advisory lock)."""

    def _corpus(self, tmp_path):
        write_migration(tmp_path, 1, "CREATE TABLE t_one (id INT);")
        write_migration(
            tmp_path,
            2,
            "-- runner:manual\n"
            "-- runner:postcondition SELECT to_regclass('t_one') IS NULL\n"
            "DROP TABLE t_one;",
            name="gated",
        )
        write_migration(tmp_path, 3, "CREATE TABLE t_three (id INT);")

    @pytest.mark.parametrize("manual", [[], ["--manual", "001"]])
    def test_backup_confirmation_refuses_anything_except_manual_002(
        self, tmp_path, capsys, manual
    ):
        with pytest.raises(SystemExit) as caught:
            main(
                [
                    "--database-url",
                    "postgresql://unused.invalid/unused",
                    "--migrations-dir",
                    str(tmp_path),
                    "apply",
                    *manual,
                    "--confirm-archive-backup",
                ]
            )
        assert caught.value.code == 2
        assert "requires --manual 002" in capsys.readouterr().err

    def test_apply_owes_the_manual_file_and_applies_the_ordinary_ones_above_it(
        self, scratch_db, tmp_path
    ):
        """THE LOAD-BEARING HALF: with the manual 002 pending, the later DDL file — numbered
        above it — applies and nothing raises. Without the exemption every
        predeploy after the next ordinary file would raise on every push."""
        self._corpus(tmp_path)

        report = apply_pending(scratch_db, tmp_path)

        assert [m.version for m in report.applied] == [1, 3]
        assert [m.version for m in report.owed] == [2]
        assert [row[0] for row in fetch_ledger(scratch_db)] == [1, 3]
        assert table_exists(scratch_db, "t_one"), "the manual file must not have run"
        # THE NEXT DEPLOY, in production's exact shape: a new ordinary file lands
        # while 002 is still owed BELOW the head. The new ordinary file applies, 002 is owed
        # again, nothing raises.
        write_migration(tmp_path, 4, "CREATE TABLE t_four (id INT);")
        again = apply_pending(scratch_db, tmp_path)
        assert [m.version for m in again.applied] == [4]
        assert [m.version for m in again.owed] == [2]
        assert table_exists(scratch_db, "t_one"), "still owed, still not run"

    def test_apply_manual_applies_it_below_the_head_with_a_ledger_row(
        self, scratch_db, tmp_path
    ):
        """THE OTHER HALF: after the later DDL file is the head, `--manual 002` applies 002
        below it — the below-head rule is for files inserted under history by
        mistake, and a gated file is under it by design."""
        self._corpus(tmp_path)
        apply_pending(scratch_db, tmp_path)

        report = apply_manual(scratch_db, tmp_path, 2)

        assert [m.version for m in report.applied] == [2]
        assert not table_exists(scratch_db, "t_one")
        rows = fetch_ledger(scratch_db)
        assert [(r[0], r[4]) for r in rows] == [
            (1, "applied"),
            (2, "applied"),
            (3, "applied"),
        ]
        # and the next deploy owes nothing
        after = apply_pending(scratch_db, tmp_path)
        assert after.applied == [] and after.owed == []

    def test_apply_manual_applies_exactly_that_file_and_no_other_pending_one(
        self, scratch_db, tmp_path
    ):
        self._corpus(tmp_path)
        write_migration(tmp_path, 4, "-- runner:manual\nCREATE TABLE t_four (id INT);")
        apply_pending(scratch_db, tmp_path)

        apply_manual(scratch_db, tmp_path, 2)

        assert [row[0] for row in fetch_ledger(scratch_db)] == [1, 2, 3]
        assert not table_exists(scratch_db, "t_four")

    def test_status_lists_it_as_owed_not_pending(self, scratch_db, tmp_path):
        self._corpus(tmp_path)
        apply_pending(scratch_db, tmp_path)

        report = status(scratch_db, tmp_path)

        assert [m.version for m, _row_status in report.applied] == [1, 3]
        assert report.pending == []
        assert [m.version for m in report.owed] == [2]

    def test_apply_manual_refuses_a_file_without_the_directive(
        self, scratch_db, tmp_path
    ):
        """`--manual` is for gated files only: an ordinary pending file is
        `apply`'s, and running it by name would bypass the order."""
        self._corpus(tmp_path)
        with pytest.raises(MigrationRunnerError, match="001") as exc:
            apply_manual(scratch_db, tmp_path, 1)
        assert "runner:manual" in str(exc.value)
        assert not table_exists(scratch_db, "t_one"), "the refusal ran nothing"
        # and after `apply` has recorded it, the directive check still fires first
        apply_pending(scratch_db, tmp_path)
        with pytest.raises(MigrationRunnerError, match="runner:manual"):
            apply_manual(scratch_db, tmp_path, 1)

    def test_apply_manual_refuses_to_run_over_a_pending_ordinary_file_below_it(
        self, scratch_db, tmp_path
    ):
        """The tree a gated file was written against has everything before it
        applied; with an ordinary 001 still pending the door refuses and names
        it, and `apply` is the way through."""
        self._corpus(tmp_path)
        with pytest.raises(MigrationRunnerError, match="002") as exc:
            apply_manual(scratch_db, tmp_path, 2)
        assert "001" in str(exc.value) and "run `apply` first" in str(exc.value)
        assert fetch_ledger(scratch_db) == []

    def test_apply_manual_refuses_a_version_that_is_not_in_the_tree(
        self, scratch_db, tmp_path
    ):
        self._corpus(tmp_path)
        with pytest.raises(MigrationRunnerError, match="009"):
            apply_manual(scratch_db, tmp_path, 9)

    def test_apply_manual_refuses_a_version_already_recorded(
        self, scratch_db, tmp_path
    ):
        self._corpus(tmp_path)
        apply_pending(scratch_db, tmp_path)
        apply_manual(scratch_db, tmp_path, 2)
        with pytest.raises(MigrationRunnerError, match="002") as exc:
            apply_manual(scratch_db, tmp_path, 2)
        assert "already" in str(exc.value)

    def test_a_false_postcondition_rolls_the_manual_file_back(
        self, scratch_db, tmp_path
    ):
        write_migration(
            tmp_path,
            1,
            "-- runner:manual\n"
            "-- runner:postcondition SELECT false\n"
            "CREATE TABLE t_never (id INT);",
        )
        with pytest.raises(MigrationRunnerError, match="001"):
            apply_manual(scratch_db, tmp_path, 1)
        assert not table_exists(scratch_db, "t_never")
        assert fetch_ledger(scratch_db) == []

    def test_the_cli_reports_owed_files_and_exits_zero(
        self, scratch_db, tmp_path, capsys
    ):
        self._corpus(tmp_path)
        argv = ["--database-url", scratch_db, "--migrations-dir", str(tmp_path)]

        assert main(argv + ["apply"]) == 0
        out = capsys.readouterr().out
        assert "applied 001" in out and "applied 003" in out
        assert "owed (manual) 002 (002_gated.sql)" in out

        assert main(argv + ["status"]) == 0
        assert "owed (manual) 002 (002_gated.sql)" in capsys.readouterr().out

        assert main(argv + ["apply", "--manual", "2"]) == 0
        assert "applied 002" in capsys.readouterr().out

        assert main(argv + ["apply", "--manual", "3"]) == 1
        assert "runner:manual" in capsys.readouterr().err


class TestTheInitialMarker:
    """`000_initial.sql` is empty and never executed; `adopt` records it on a
    database built from the schema code, and later files apply on top."""

    def test_an_empty_initial_marker_is_discovered_with_no_statements(self, tmp_path):
        (tmp_path / "000_initial.sql").write_bytes(b"")
        [m] = discover_migrations(tmp_path)
        assert m.version == INITIAL_VERSION
        assert m.statements == ()

    def test_a_byte_in_the_initial_marker_is_refused_at_discovery(self, tmp_path):
        (tmp_path / "000_initial.sql").write_text("SELECT 1;")
        with pytest.raises(
            MigrationRunnerError, match=r"000 \(000_initial\.sql\) must be empty"
        ):
            discover_migrations(tmp_path)

    def test_an_unrecorded_initial_marker_is_refused_never_applied(
        self, scratch_db, tmp_path
    ):
        (tmp_path / "000_initial.sql").write_bytes(b"")
        write_migration(tmp_path, 1, "CREATE TABLE t_a (id INT);")
        with pytest.raises(
            MigrationRunnerError, match="not recorded: build the schema"
        ):
            apply_pending(scratch_db, tmp_path)
        assert fetch_ledger(scratch_db) == []
        assert not table_exists(scratch_db, "t_a")

    def test_adopt_records_the_marker_then_apply_runs_what_is_above_it(
        self, scratch_db, tmp_path
    ):
        (tmp_path / "000_initial.sql").write_bytes(b"")
        write_migration(
            tmp_path,
            1,
            "-- runner:postcondition SELECT to_regclass('public.t_a') IS NOT NULL\n"
            "CREATE TABLE t_a (id INT);",
        )
        manifest = tmp_path / "adoption_manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "required_through": 0,
                    "entries": [{"version": 0, "probe": "SELECT true"}],
                }
            )
        )
        report = adopt(scratch_db, tmp_path, manifest)
        assert [m.version for m in report.adopted] == [0]
        assert [m.version for m in report.pending] == [1]
        applied = apply_pending(scratch_db, tmp_path)
        assert [m.version for m in applied.applied] == [1]
        assert [row[0] for row in fetch_ledger(scratch_db)] == [0, 1]
        again = adopt(scratch_db, tmp_path, manifest)
        assert [m.version for m in again.already] == [0, 1]
        assert again.adopted == [] and again.pending == []
