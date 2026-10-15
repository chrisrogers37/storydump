"""Explicit-path dump/restore coverage requiring a second disposable cluster.

Set CUTOVER_REHEARSAL_CODE_DSN to an independent, otherwise unused cluster
without service roles. The ordinary test database settings name the restore
cluster. Both administrators must have matching attributes and privileges.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from scripts import cutover_rehearsal as rehearsal
from scripts.migration_runner import LEDGER_DDL, MIGRATIONS_DIR
from scripts.schema_parity import schema_signature
from src.models.target.ddl import build
from tests.scripts.conftest import _scratch


def database_names(admin):
    with admin.cursor() as cur:
        cur.execute("SELECT datname FROM pg_database ORDER BY 1")
        return cur.fetchall()


def service_globals(admin):
    with admin.cursor() as cur:
        cur.execute(
            "SELECT to_jsonb(r) FROM pg_roles r WHERE left(rolname,4)='svc_' ORDER BY rolname"
        )
        roles = cur.fetchall()
        cur.execute(
            "SELECT to_jsonb(m) FROM pg_auth_members m ORDER BY roleid, member, grantor"
        )
        return roles, cur.fetchall()


@pytest.mark.parametrize(
    "extra_table", [False, True], ids=["matching", "catalog-difference"]
)
def test_complete_dump_rehearsal(admin_conn, tmp_path, extra_table):
    import psycopg2

    code_dsn = os.environ.get("CUTOVER_REHEARSAL_CODE_DSN")
    if not code_dsn:
        pytest.skip("set CUTOVER_REHEARSAL_CODE_DSN for the explicit dump/restore test")
    code_admin = psycopg2.connect(code_dsn)
    code_admin.autocommit = True
    lifecycle = _scratch(admin_conn, roles=[])
    source_dsn = next(lifecycle)
    try:
        rehearsal.require_cluster_separation(admin_conn, code_admin)
        assert rehearsal._service_roles(code_admin) == set(), (
            "the code cluster must start without svc_* roles"
        )
        engine = create_engine(source_dsn)
        try:
            with engine.connect() as conn:
                build(conn)
        finally:
            engine.dispose()
        conn = psycopg2.connect(source_dsn)
        try:
            with conn, conn.cursor() as cur:
                cur.execute(
                    "ALTER TABLE public.post_intents ADD COLUMN legacy_queue_item_id uuid"
                )
                cur.execute(
                    "ALTER TABLE public.post_intents DROP CONSTRAINT ck_intent_via, DROP CONSTRAINT ck_posted_complete"
                )
                cur.execute(
                    "ALTER TABLE public.post_intents ADD CONSTRAINT ck_intent_via CHECK (published_via IS NULL OR published_via IN ('api','manual','dryrun','legacy_backfill')), ADD CONSTRAINT ck_posted_complete CHECK (state <> 'posted' OR published_via IN ('api','manual','legacy_backfill'))"
                )
                cur.execute(
                    "COMMENT ON FUNCTION public.fn_safe_tz(text) IS 'cutover fixture'"
                )
                cur.execute(LEDGER_DDL)
                for version, checksum in (
                    (
                        1,
                        "f75f8a5b999673786d5bc79fca214a6bc7cc7360104fc6ab7703d440f03767c1",
                    ),
                    (
                        2,
                        "8e2d5502c1f1ba613c598982824f9588d94f8a3a7fc167df36f310d8cfc136bc",
                    ),
                    (
                        149,
                        "82056a5dc500da085ff1ddbe33f0cff219a5d08b463bf8205fcca20159c0593c",
                    ),
                ):
                    cur.execute(
                        "INSERT INTO runner.schema_migrations(version, checksum, applied_by, execution_ms, status) VALUES (%s, %s, current_user, 1, 'applied')",
                        (version, checksum),
                    )
                cur.execute(
                    "CREATE TABLE public.schema_version (version integer); INSERT INTO public.schema_version VALUES (149)"
                )
                snapshot_sql = (
                    MIGRATIONS_DIR / "002_drop_archive_snapshots.sql"
                ).read_text()
                names = re.findall(
                    r"DROP TABLE IF EXISTS archive\.([a-z_0-9]+);", snapshot_sql
                )
                assert len(names) == 16
                for name in names:
                    cur.execute(
                        f'CREATE TABLE archive."{name}" AS SELECT 37 AS sentinel'
                    )
                if extra_table:
                    cur.execute(
                        "CREATE TABLE public.unexpected_cutover_object (id integer)"
                    )
        finally:
            conn.close()
        dump = tmp_path / "production-shaped.dump"
        source = tmp_path / "source-catalog.json"
        source.write_text(json.dumps(schema_signature(source_dsn)), encoding="utf-8")
        subprocess.run(
            ["pg_dump", "--format=custom", "--file", str(dump), source_dsn],
            check=True,
            capture_output=True,
        )
        source_databases, code_databases = (
            database_names(admin_conn),
            database_names(code_admin),
        )
        globals_before = service_globals(admin_conn)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.cutover_rehearsal",
                "--dump",
                str(dump),
                "--source-catalog",
                str(source),
                "--confirm-disposable-clusters",
                "--confirm-archive-backup",
            ],
            cwd=Path(__file__).resolve().parents[2],
            env={
                **os.environ,
                "REHEARSAL_RESTORE_ADMIN_DSN": source_dsn,
                "REHEARSAL_CODE_ADMIN_DSN": code_dsn,
                "SCHEMA_DRIFT_DSN": "",
                "REQUIRE_SCHEMA_DRIFT_CHECK": "0",
            },
            capture_output=True,
            text=True,
        )
        assert result.returncode == int(extra_table), result.stdout + result.stderr
        assert "pre-deploy: 0 applied; 001 and 002 owed (manual)" in result.stdout, (
            result.stdout + result.stderr
        )
        assert (
            "converted legacy runner ledger to runner.schema_migrations_legacy"
            in result.stdout
        )
        assert result.stdout.index("initial parity (diagnostic)") < result.stdout.index(
            "applied owner step 001"
        )
        assert "preserved public history: runner.legacy_schema_version" in result.stdout
        if extra_table:
            assert "public.unexpected_cutover_object" in result.stdout
            assert "catalog parity failed" in result.stderr
        else:
            assert "cutover rehearsal passed: zero catalog differences" in result.stdout
        assert database_names(admin_conn) == source_databases
        assert database_names(code_admin) == code_databases
        assert service_globals(admin_conn) == globals_before
        assert rehearsal._service_roles(code_admin) == set()
    finally:
        lifecycle.close()
        code_admin.close()
