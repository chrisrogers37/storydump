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
    apply_manual,
    apply_pending,
    discover_migrations,
    main,
    status,
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


class TestMarkers:
    """`runner:` lines are the file's contract with the runner, so a marker the
    runner does not know is a hard failure at discovery — a misspelt
    `runner:manaul` would otherwise be an ordinary file applied at the next
    deploy (the tear-out, phase 03; fork F6)."""

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
            "-- runner:unadvertised because it snapshots",  # a flag with an argument
        ],
    )
    def test_every_spelling_that_reads_as_a_marker_reaches_the_known_set(
        self, tmp_path, line
    ):
        """An UNKNOWN word (`gated`, `MANUAL`) in every frame the regex
        tolerates is refused at the known-set check — the frame is lenient,
        the word is exact. (Phase 03 wrote these with `manual` as the unknown
        word; phase 04 made it a known one.)"""
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
            "-- The runner:schema-move marker is what makes the boundary derivable.\n"
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

    def test_unadvertised_is_read_off_the_file(self, tmp_path):
        write_migration(
            tmp_path, 1, "-- runner:unadvertised\nCREATE TABLE t_a (id INT);"
        )
        write_migration(tmp_path, 2, "CREATE TABLE t_b (id INT);")
        a, b = discover_migrations(tmp_path)
        assert a.unadvertised is True
        assert b.unadvertised is False

    def test_an_unadvertised_file_is_applied_like_any_other(self, scratch_db, tmp_path):
        write_migration(
            tmp_path, 1, "-- runner:unadvertised\nCREATE TABLE t_a (id INT);"
        )
        apply_pending(scratch_db, tmp_path)
        assert [row[0] for row in fetch_ledger(scratch_db)] == [1]

    @pytest.mark.parametrize(
        "line",
        [
            "--- runner:manual",  # three dashes
            "-- -- runner:manual",  # an editor's "comment this line" on a comment
            "/* runner:manual */",  # a block-comment frame
            "# runner:manual",  # a shell-style frame
            "-- runner manual",  # no colon
            "DROP TABLE t_gone; -- runner:manual",  # after code on the same line
            "-- RUNNER unadvertised",  # another word, upper case, no colon
            "-- runner-manual",  # a dash for the colon
            "-- runner=manual",  # an equals sign for the colon
        ],
    )
    def test_a_near_miss_is_refused_never_read_as_prose(self, tmp_path, line):
        """Each of these once read as prose — for a `manual` file the whole
        hazard: an ordinary file the next predeploy applies. The frame the
        grammar reads is `-- runner:<word>` alone at the start of its line;
        anything that LOOKS like an attempt and is not that is refused."""
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
            "-- The runner:schema-move marker is what makes the boundary derivable.\n"
            "-- python -m scripts.migration_runner apply --manual 79 is the door.\n"
            "CREATE TABLE t_a (id INT);",
        )
        [m] = discover_migrations(tmp_path)
        assert m.manual is False and m.schema_move is False

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


class TestManual:
    """`-- runner:manual` (the tear-out, phase 04; fork F6): a file the deploy
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

    def test_apply_owes_the_manual_file_and_applies_the_ordinary_ones_above_it(
        self, scratch_db, tmp_path
    ):
        """THE LOAD-BEARING HALF: with the manual 002 pending, 003 — numbered
        above it — applies and nothing raises. Without the exemption every
        predeploy after the next ordinary file would raise on every push."""
        self._corpus(tmp_path)

        report = apply_pending(scratch_db, tmp_path)

        assert [m.version for m in report.applied] == [1, 3]
        assert [m.version for m in report.owed] == [2]
        assert [row[0] for row in fetch_ledger(scratch_db)] == [1, 3]
        assert table_exists(scratch_db, "t_one"), "the manual file must not have run"
        # THE NEXT DEPLOY, in production's exact shape: an ordinary 004 lands
        # while 002 is still owed BELOW the head — the state the old rule
        # raised on, on every push, for both services. 004 applies, 002 is
        # owed again, nothing raises.
        write_migration(tmp_path, 4, "CREATE TABLE t_four (id INT);")
        again = apply_pending(scratch_db, tmp_path)
        assert [m.version for m in again.applied] == [4]
        assert [m.version for m in again.owed] == [2]
        assert table_exists(scratch_db, "t_one"), "still owed, still not run"

    def test_apply_manual_applies_it_below_the_head_with_a_ledger_row(
        self, scratch_db, tmp_path
    ):
        """THE OTHER HALF: after 003 is the head, `--manual 002` applies 002
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
