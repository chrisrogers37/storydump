"""A restored cutover database must pass the full catalog comparison."""

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import psycopg2
import pytest

from scripts import cutover_rehearsal as rehearsal
from tests.scripts.conftest import _scratch, build_world, fetch_one
from tests.unreadable_urls import (
    LIBPQ_UNREADABLE,
    READABLE,
    REFUSAL,
    STAND_IN,
    assert_refused_by_name,
    assert_withheld,
    cases,
    stand_in_administrator,
)


@pytest.fixture
def catalog_pair(world_db, admin_conn, owner_actor):
    lifecycle = _scratch(admin_conn, owner=owner_actor)
    second = next(lifecycle)
    try:
        yield world_db, build_world(second, owner_actor, admin_conn)
    finally:
        lifecycle.close()


def test_parity_difference_stops_rehearsal(catalog_pair, capsys):
    restored, expected = catalog_pair
    with psycopg2.connect(restored) as conn, conn.cursor() as cur:
        cur.execute("CREATE TABLE public.unexpected_cutover_object (id integer)")

    with pytest.raises(RuntimeError, match="catalog parity failed"):
        rehearsal.require_parity(restored, expected)

    output = capsys.readouterr().out
    assert "public.unexpected_cutover_object" in output
    assert "difference(s)" in output


def test_matching_catalogs_pass_rehearsal_gate(catalog_pair, capsys):
    restored, expected = catalog_pair
    rehearsal.require_parity(restored, expected)
    assert "0 difference(s)" in capsys.readouterr().out


def test_public_history_preservation_leaves_runner_conversion_to_its_command(
    scratch_db,
):
    with psycopg2.connect(scratch_db) as conn, conn.cursor() as cur:
        cur.execute("CREATE SCHEMA runner")
        cur.execute("CREATE TABLE runner.schema_migrations (version integer)")
        cur.execute("INSERT INTO runner.schema_migrations VALUES (149)")
        cur.execute("CREATE TABLE public.schema_version (version integer)")
        cur.execute("INSERT INTO public.schema_version VALUES (149)")
        cur.execute(
            "CREATE TABLE public.schema_migrations (version integer, checksum text)"
        )
        cur.execute(
            "INSERT INTO public.schema_migrations VALUES (149, 'retired-history')"
        )

    preserved = rehearsal.preserve_public_legacy_ledgers(scratch_db)

    assert preserved == [
        "runner.legacy_public_schema_migrations",
        "runner.legacy_schema_version",
    ]
    assert fetch_one(
        scratch_db, "SELECT * FROM runner.legacy_public_schema_migrations"
    ) == (149, "retired-history")
    assert fetch_one(scratch_db, "SELECT * FROM runner.legacy_schema_version") == (149,)
    assert fetch_one(scratch_db, "SELECT * FROM runner.schema_migrations") == (149,)
    assert fetch_one(
        scratch_db, "SELECT to_regclass('runner.schema_migrations_legacy')"
    ) == (None,)


def test_public_history_preservation_refuses_to_overwrite_history(scratch_db):
    with psycopg2.connect(scratch_db) as conn, conn.cursor() as cur:
        cur.execute("CREATE SCHEMA runner")
        cur.execute("CREATE TABLE public.schema_version (version integer)")
        cur.execute("CREATE TABLE runner.legacy_schema_version (version integer)")

    with pytest.raises(RuntimeError, match="already exists"):
        rehearsal.preserve_public_legacy_ledgers(scratch_db)
    assert fetch_one(scratch_db, "SELECT to_regclass('public.schema_version')") == (
        "schema_version",
    )


def test_required_live_drift_dsn_cannot_be_silently_omitted(monkeypatch, capsys):
    monkeypatch.setenv("REQUIRE_SCHEMA_DRIFT_CHECK", "1")
    monkeypatch.delenv("SCHEMA_DRIFT_DSN", raising=False)
    assert rehearsal.main(["--check-drift", "--against", "unused"]) == 1
    assert "SCHEMA_DRIFT_DSN" in capsys.readouterr().err


def test_drift_only_mode_propagates_catalog_difference(
    catalog_pair, monkeypatch, capsys
):
    restored, expected = catalog_pair
    with psycopg2.connect(restored) as conn, conn.cursor() as cur:
        cur.execute("CREATE TABLE public.unexpected_cutover_object (id integer)")
    monkeypatch.setenv("SCHEMA_DRIFT_DSN", restored)
    monkeypatch.setenv("REQUIRE_SCHEMA_DRIFT_CHECK", "1")

    assert rehearsal.main(["--check-drift", "--against", expected]) == 1
    assert "catalog parity failed" in capsys.readouterr().err


@pytest.fixture
def no_drift_check(monkeypatch):
    monkeypatch.delenv("SCHEMA_DRIFT_DSN", raising=False)
    monkeypatch.delenv("REQUIRE_SCHEMA_DRIFT_CHECK", raising=False)


#: How the rehearsal names its two administrator URLs in a refusal.
RESTORE_ADMIN = "REHEARSAL_RESTORE_ADMIN_DSN (or --restore-admin-url)"
CODE_ADMIN = "REHEARSAL_CODE_ADMIN_DSN (or --code-admin-url)"


@pytest.mark.parametrize("dsn", **cases(LIBPQ_UNREADABLE))
def test_an_unreadable_drift_dsn_is_refused_by_name(
    dsn, no_drift_check, monkeypatch, capsys
):
    """libpq quotes the part of a URL it could not read, which can be the
    password. The rehearsal names the variable instead: the one an operator
    set, not the runner's flag it hands the URL to. No database: the drift
    DSN is read first."""
    monkeypatch.setenv("SCHEMA_DRIFT_DSN", dsn)

    assert rehearsal.main(["--check-drift", "--against", READABLE]) == 1

    out, err = capsys.readouterr()
    assert f"cutover rehearsal failed: SCHEMA_DRIFT_DSN {REFUSAL}" in err
    assert "catalog parity failed" not in err, "an unread URL is not a difference"
    assert_withheld(out, err)

    with pytest.raises(ValueError) as caught:
        rehearsal.require_parity(dsn, READABLE, restored_name=rehearsal.DRIFT_NAME)
    assert_refused_by_name(caught.value, "SCHEMA_DRIFT_DSN")


@pytest.mark.integration
def test_an_unreadable_against_url_is_refused_by_name(
    scratch_db, no_drift_check, monkeypatch, capsys
):
    """The drift catalog is read from a real database, so the refusal is the
    second URL's."""
    monkeypatch.setenv("SCHEMA_DRIFT_DSN", scratch_db)

    against = LIBPQ_UNREADABLE["raw-slash"]
    assert rehearsal.main(["--check-drift", "--against", against]) == 1

    out, err = capsys.readouterr()
    assert f"cutover rehearsal failed: --against {REFUSAL}" in err
    assert_withheld(out, err)

    with pytest.raises(ValueError) as caught:
        rehearsal.require_parity(
            scratch_db,
            against,
            restored_name=rehearsal.DRIFT_NAME,
            code_name="--against",
        )
    assert_refused_by_name(caught.value, "--against")


@pytest.fixture
def artifacts(tmp_path):
    """Stand-in artifacts: the administrators connect before anything reads
    them."""
    dump = tmp_path / "cutover.dump"
    dump.touch()
    catalog = tmp_path / "catalog.json"
    catalog.write_text("{}", encoding="utf-8")
    return dump, catalog


def _rehearsal(artifacts, restore_admin_url, code_admin_url):
    """The whole rehearsal's arguments."""
    dump, catalog = artifacts
    return [
        "--dump",
        str(dump),
        "--source-catalog",
        str(catalog),
        "--restore-admin-url",
        restore_admin_url,
        "--code-admin-url",
        code_admin_url,
        "--confirm-disposable-clusters",
        "--confirm-archive-backup",
    ]


def _refusal(artifacts, restore_admin_url, code_admin_url):
    """The whole rehearsal's refusal, run as a function so it can be read
    whole: its message, its chain and its traceback."""
    dump, catalog = artifacts
    with pytest.raises(ValueError) as caught:
        rehearsal.run(
            dump,
            restore_admin_url,
            code_admin_url,
            source_catalog=catalog,
            confirm_archive_backup=True,
        )
    return caught.value


@pytest.mark.parametrize("dsn", **cases(LIBPQ_UNREADABLE))
def test_an_unreadable_restore_administrator_dsn_is_refused_by_name(
    dsn, artifacts, no_drift_check, capsys
):
    """No database: the restore cluster's administrator connects first."""
    assert rehearsal.main(_rehearsal(artifacts, dsn, READABLE)) == 1

    out, err = capsys.readouterr()
    assert f"cutover rehearsal failed: {RESTORE_ADMIN} {REFUSAL}" in err
    assert_withheld(out, err)
    assert_refused_by_name(_refusal(artifacts, dsn, READABLE), RESTORE_ADMIN)


def test_an_unreadable_code_administrator_dsn_is_refused_by_name(
    artifacts, no_drift_check, monkeypatch, capsys
):
    """The restore cluster's administrator is a stand-in, so the refusal is
    the code cluster's, and the stand-in is still closed."""
    stand_in = stand_in_administrator(monkeypatch)

    code = LIBPQ_UNREADABLE["raw-at-sign"]
    assert rehearsal.main(_rehearsal(artifacts, STAND_IN, code)) == 1

    out, err = capsys.readouterr()
    assert f"cutover rehearsal failed: {CODE_ADMIN} {REFUSAL}" in err
    assert_withheld(out, err)
    assert_refused_by_name(_refusal(artifacts, STAND_IN, code), CODE_ADMIN)
    assert stand_in.closed


def test_whole_rehearsal_rejects_shared_cluster(admin_conn):
    with pytest.raises(ValueError, match="independent PostgreSQL clusters"):
        rehearsal.require_cluster_separation(admin_conn, admin_conn)


def test_restore_fidelity_refuses_lost_database_privileges(world_db, tmp_path, capsys):
    from scripts.schema_parity import schema_signature

    source = tmp_path / "source-catalog.json"
    source.write_text(json.dumps(schema_signature(world_db)), encoding="utf-8")
    with psycopg2.connect(world_db) as conn, conn.cursor() as cur:
        from psycopg2 import sql

        cur.execute("SELECT current_database()")
        name = cur.fetchone()[0]
        cur.execute(
            sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(
                sql.Identifier(name)
            )
        )

    with pytest.raises(RuntimeError, match="dump restore fidelity failed"):
        rehearsal.require_restore_fidelity(world_db, source)
    assert "database <database>.acl" in capsys.readouterr().out


def test_source_database_settings_and_grants_are_restored(catalog_pair, admin_conn):
    from psycopg2 import sql
    from psycopg2.extensions import parse_dsn

    from scripts.schema_parity import schema_diff, schema_signature

    source_dsn, restored_dsn = catalog_pair
    source_name = parse_dsn(source_dsn)["dbname"]
    restored_name = parse_dsn(restored_dsn)["dbname"]
    outside_role = admin_conn.info.user
    with psycopg2.connect(source_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(
                sql.Identifier(source_name)
            )
        )
        cur.execute(
            sql.SQL("ALTER DATABASE {} SET timezone TO 'UTC'").format(
                sql.Identifier(source_name)
            )
        )
    with admin_conn.cursor() as cur:
        cur.execute(
            sql.SQL("ALTER DATABASE {} SET search_path TO public, pg_temp").format(
                sql.Identifier(source_name)
            )
        )
        cur.execute(
            sql.SQL("GRANT CREATE ON DATABASE {} TO {} WITH GRANT OPTION").format(
                sql.Identifier(source_name), sql.Identifier(outside_role)
            )
        )
        for role in ("svc_worker", outside_role):
            cur.execute(
                sql.SQL(
                    "ALTER ROLE {} IN DATABASE {} SET search_path TO public, pg_temp"
                ).format(sql.Identifier(role), sql.Identifier(source_name))
            )
    source = schema_signature(source_dsn)
    with admin_conn.cursor() as cur:
        cur.execute("SHOW search_path")
        original_search_path = cur.fetchone()

    rehearsal.restore_database_metadata(admin_conn, restored_name, source)

    with admin_conn.cursor() as cur:
        cur.execute("SHOW search_path")
        assert cur.fetchone() == original_search_path
    assert schema_diff(source, schema_signature(restored_dsn)) == []
    assert source["reported"]["outside_roles"]["database.<database>"]
    restored = schema_signature(restored_dsn)
    assert restored["reported"]["outside_roles"] == source["reported"]["outside_roles"]


def documented_rollback_sql():
    runbook = (
        Path(__file__).resolve().parents[2]
        / "documentation/operations/schema-cutover.md"
    )
    blocks = [
        block
        for block in re.findall(r"```sql\n(.*?)```", runbook.read_text(), re.S)
        if "schema_migrations_cutover_attempt" in block
    ]
    assert len(blocks) == 1, "the cutover runbook needs an executable ledger rollback"
    return blocks[0]


def documented_constraint_preflight_sql():
    runbook = (
        Path(__file__).resolve().parents[2]
        / "documentation/operations/schema-cutover.md"
    )
    blocks = [
        block
        for block in re.findall(r"```sql\n(.*?)```", runbook.read_text(), re.S)
        if "ck_posted_complete_violations" in block
    ]
    assert len(blocks) == 1, "the cutover runbook needs one constraint preflight"
    return blocks[0]


def test_documented_constraint_preflight_counts_false_checks_in_read_only_tx(
    scratch_db,
):
    with psycopg2.connect(scratch_db) as conn, conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE public.post_intents ("
            "state text, published_via text, cap_consumed_on timestamptz, "
            "publish_step text, ig_container_id text)"
        )
        cur.execute(
            "INSERT INTO public.post_intents VALUES "
            "('posted', 'manual', now(), NULL, NULL), "
            "('queued', 'legacy_backfill', NULL, NULL, NULL), "
            "('posted', NULL, now(), NULL, NULL), "
            "('posted', 'manual', now(), NULL, NULL), "
            "('posted', 'manual', NULL, NULL, NULL)"
        )
    before_rows = fetch_one(scratch_db, "SELECT count(*) FROM public.post_intents")
    sql = documented_constraint_preflight_sql()
    with psycopg2.connect(scratch_db) as conn, conn.cursor() as cur:
        statements = [
            statement.strip() for statement in sql.split(";") if statement.strip()
        ]
        cur.execute(statements[0])
        assert statements[0].upper() == "BEGIN READ ONLY"
        cur.execute(statements[1])
        assert cur.fetchone() == (1, 1)
        cur.execute("SELECT current_setting('transaction_read_only')")
        assert cur.fetchone() == ("on",)
        cur.execute(statements[2])
    assert (
        fetch_one(scratch_db, "SELECT count(*) FROM public.post_intents") == before_rows
    )


def test_runbook_orders_neon_branch_rehearsal_before_production():
    runbook = (
        Path(__file__).resolve().parents[2]
        / "documentation/operations/schema-cutover.md"
    )
    text = runbook.read_text()
    hold_heading = "## Hold Railway deployments before integrating main"
    assert hold_heading in text
    assert "## Rehearse on an owner-created Neon branch" in text
    assert "## Repeat on production" in text
    branch_start = text.index("## Rehearse on an owner-created Neon branch")
    production_start = text.index("## Repeat on production")
    hold_start = text.index(hold_heading)
    assert hold_start < branch_start
    hold = text[
        hold_start : text.index("## Preserve the database and archive snapshots")
    ]
    assert "both Railway services" in hold
    assert "`worker`" in hold and "`storydump`" in hold
    assert hold.index("Pause automatic deployments") < hold.index(
        "Confirm no pre-deploy is in flight"
    )
    assert hold.index("Confirm no pre-deploy is in flight") < hold.index(
        "Integrate the foundation and cutover-safety changes into `main`"
    )
    assert "convert-ledger" in hold and "runner:manual" in hold
    assert "Keep both services' automatic deployments paused" in hold
    assert branch_start < production_start
    branch = text[branch_start:production_start]
    assert 'CUTOVER_DSN="$CUTOVER_BRANCH_DSN"' in branch
    preflight = branch.index("ck_posted_complete_violations")
    position = 0
    for command in (
        r"convert-ledger --confirm-legacy-ledger",
        r'--database-url "\$CUTOVER_DSN" apply\s*\n',
        r'parity --against "\$SCHEMA_CODE_DSN"',
        r"ck_posted_complete_violations",
        r"apply --manual 001",
        r"validate_catalog_constraints\.sql",
        r"apply --manual 002 --confirm-archive-backup",
        r'parity --against "\$SCHEMA_CODE_DSN"',
    ):
        match = re.search(command, branch[position:])
        assert match, f"cutover command missing or out of order: {command}"
        position += match.end()
    assert preflight < branch.index("apply --manual 001")
    assert "initial parity is diagnostic" in branch.lower()
    assert "zero differences" in branch.lower()
    assert "Migration 001 is already" in branch
    assert "do not rename" in branch and "repair the ledger" in branch
    assert "membership grantors" in branch
    assert "effective role-to-member edges" in branch
    assert "Do not grant" in branch and "admin powers" in branch
    assert 'CUTOVER_DSN="$PRODUCTION_OWNER_DSN"' in text[production_start:]
    assert "Deploy only the fully integrated commit" in text[production_start:]
    assert "final parity reports zero differences" in text[production_start:]
    assert "before owner-applied migrations" in text


def prepare_converted_ledger(dsn, *, applied=False):
    from scripts.migration_runner import LEDGER_DDL

    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(LEDGER_DDL)
        cur.execute(
            "CREATE TABLE runner.schema_migrations_legacy (version integer, checksum text)"
        )
        cur.execute(
            "INSERT INTO runner.schema_migrations_legacy VALUES (149, 'retired-history')"
        )
        cur.execute(
            "CREATE TABLE runner.legacy_schema_version (version integer); INSERT INTO runner.legacy_schema_version VALUES (149)"
        )
        cur.execute(
            "CREATE TABLE runner.legacy_public_schema_migrations (version integer); INSERT INTO runner.legacy_public_schema_migrations VALUES (149)"
        )
        cur.execute(
            "INSERT INTO runner.schema_migrations(version, checksum, applied_by, status) VALUES (0, 'initial-checksum', current_user, 'adopted')"
        )
        if applied:
            cur.execute(
                "INSERT INTO runner.schema_migrations(version, checksum, applied_by, status) VALUES (1, 'catalog-checksum', current_user, 'applied')"
            )


def test_documented_rollback_restores_legacy_and_keeps_new_history(scratch_db):
    rollback = documented_rollback_sql()
    prepare_converted_ledger(scratch_db)
    with psycopg2.connect(scratch_db) as conn, conn.cursor() as cur:
        cur.execute(rollback)
    assert fetch_one(scratch_db, "SELECT * FROM runner.schema_migrations") == (
        149,
        "retired-history",
    )
    assert fetch_one(
        scratch_db,
        "SELECT version, status FROM runner.schema_migrations_cutover_attempt",
    ) == (0, "adopted")
    assert fetch_one(scratch_db, "SELECT * FROM public.schema_version") == (149,)
    assert fetch_one(scratch_db, "SELECT * FROM public.schema_migrations") == (149,)


@pytest.mark.parametrize("applied", [True, False], ids=["owner-applied", "empty"])
def test_documented_rollback_refuses_unexpected_history(scratch_db, applied):
    rollback = documented_rollback_sql()
    prepare_converted_ledger(scratch_db, applied=applied)
    if not applied:
        with psycopg2.connect(scratch_db) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM runner.schema_migrations")
    with psycopg2.connect(scratch_db) as conn, conn.cursor() as cur:
        with pytest.raises(psycopg2.Error, match="before owner-applied migrations"):
            cur.execute(rollback)
    assert fetch_one(scratch_db, "SELECT count(*) FROM runner.schema_migrations") == (
        2 if applied else 0,
    )
    assert fetch_one(scratch_db, "SELECT * FROM runner.schema_migrations_legacy") == (
        149,
        "retired-history",
    )
    assert fetch_one(
        scratch_db, "SELECT to_regclass('runner.schema_migrations_cutover_attempt')"
    ) == (None,)


def test_documented_rollback_serializes_with_owner_migration(scratch_db):
    from scripts.migration_runner import RUNNER_LOCK_KEY

    rollback = documented_rollback_sql()
    prepare_converted_ledger(scratch_db)
    writer = psycopg2.connect(scratch_db)
    writer.autocommit = True
    started, backend = threading.Event(), []
    pool = ThreadPoolExecutor(max_workers=1)
    with writer.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s)", (RUNNER_LOCK_KEY,))

    def attempt():
        with closing(psycopg2.connect(scratch_db)) as conn, conn.cursor() as cur:
            backend.append(conn.get_backend_pid())
            started.set()
            cur.execute(rollback)

    future = pool.submit(attempt)
    try:
        assert started.wait(3)
        deadline = time.monotonic() + 3
        blocked = False
        while time.monotonic() < deadline and not future.done():
            with writer.cursor() as cur:
                cur.execute(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid=%s AND locktype='advisory' AND NOT granted)",
                    (backend[0],),
                )
                blocked = cur.fetchone()[0]
            if blocked:
                break
            time.sleep(0.01)
        assert blocked, "ledger rollback did not wait for the running owner migration"
        with writer.cursor() as cur:
            cur.execute(
                "INSERT INTO runner.schema_migrations(version, checksum, applied_by, status) VALUES (1, 'catalog-checksum', current_user, 'applied')"
            )
            cur.execute("SELECT pg_advisory_unlock(%s)", (RUNNER_LOCK_KEY,))
        with pytest.raises(psycopg2.Error, match="before owner-applied migrations"):
            future.result(timeout=3)
    finally:
        with writer.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (RUNNER_LOCK_KEY,))
        writer.close()
        pool.shutdown(wait=True)
