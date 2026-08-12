"""Shared fixtures and helpers for the migration-runner suites.

Real-PostgreSQL scratch databases, one per test, dropped on teardown — never
the shared storyline_test database. Connection parameters come from the same
settings the rest of the suite uses (``.env.test`` via the root conftest), so
a credential or port move breaks every suite the same way instead of this one
differently.
"""

import json
import os
import subprocess
import uuid
from pathlib import Path

import psycopg2
import pytest
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

from src.config.settings import settings

REPO_ROOT = Path(__file__).resolve().parents[2]
SETUP_SQL = REPO_ROOT / "scripts" / "setup_database.sql"


def _dsn(database: str) -> str:
    auth = settings.DB_USER
    if settings.DB_PASSWORD:
        auth = f"{settings.DB_USER}:{settings.DB_PASSWORD}"
    return f"postgresql://{auth}@{settings.DB_HOST}:{settings.DB_PORT}/{database}"


@pytest.fixture(scope="session")
def admin_conn():
    """One session-wide connection to the maintenance database for creating
    and dropping scratch databases."""
    conn = psycopg2.connect(_dsn("postgres"))
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    yield conn
    conn.close()


def _create_db(admin_conn, name: str, template: str | None = None) -> str:
    clause = f' TEMPLATE "{template}"' if template else ""
    with admin_conn.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"{clause}')
    return _dsn(name)


def _drop_db(admin_conn, name: str) -> None:
    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
            " WHERE datname = %s AND pid <> pg_backend_pid()",
            (name,),
        )
        cur.execute(f'DROP DATABASE IF EXISTS "{name}"')


def _scratch(admin_conn, template: str | None = None):
    name = f"runner_test_{uuid.uuid4().hex[:10]}"
    dsn = _create_db(admin_conn, name, template)
    try:
        yield dsn
    finally:
        _drop_db(admin_conn, name)


@pytest.fixture()
def scratch_db(admin_conn):
    """A fresh empty database."""
    yield from _scratch(admin_conn)


@pytest.fixture()
def second_scratch_db(admin_conn):
    """An independent second database for two-schema comparisons."""
    yield from _scratch(admin_conn)


@pytest.fixture(scope="session")
def at49_template(admin_conn):
    """The production-shaped at-49 database (setup + 001–049 hand-applied,
    no ledger), built once per session as a template; tests copy it via
    CREATE DATABASE ... TEMPLATE instead of re-executing the corpus."""
    name = f"runner_tpl_at49_{uuid.uuid4().hex[:8]}"
    dsn = _create_db(admin_conn, name)
    psql_apply(dsn, [SETUP_SQL] + migration_files(49))
    yield name
    _drop_db(admin_conn, name)


@pytest.fixture()
def at49_db(admin_conn, at49_template):
    """A private copy of the at-49 shape, safe to mutate."""
    yield from _scratch(admin_conn, template=at49_template)


def migration_files(limit: int):
    """The real corpus up to ``limit``, selected by the runner's own
    discovery so the gate cannot drift from production's file set."""
    from scripts.migration_runner import discover_migrations

    files = [
        m.path
        for m in discover_migrations(REPO_ROOT / "scripts" / "migrations")
        if m.version <= limit
    ]
    assert files, "no migration files found"
    return files


def psql_apply(dsn: str, files) -> None:
    """Hand-apply SQL files the way production history was actually built."""
    cmd = ["psql", dsn, "-q", "-v", "ON_ERROR_STOP=1"]
    for f in files:
        cmd += ["-f", str(f)]
    env = dict(os.environ, PGOPTIONS="-c client_min_messages=warning")
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    assert result.returncode == 0, f"hand-apply failed: {result.stderr[-2000:]}"


# --- plain helpers shared across the runner suites ---


def write_migration(migrations_dir, version, body, name="m"):
    path = migrations_dir / f"{version:03d}_{name}.sql"
    path.write_text(body)
    return path


def fetch_ledger(dsn):
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT version, checksum, applied_by, execution_ms, status"
            " FROM runner.schema_migrations ORDER BY version"
        )
        rows = cur.fetchall()
    conn.close()
    return rows


def table_exists(dsn, table, schema="public"):
    row = fetch_one(
        dsn,
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables"
        " WHERE table_schema = %s AND table_name = %s)",
        (schema, table),
    )
    return row[0]


def execute(dsn, sql, params=None):
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
    conn.close()


def fetch_one(dsn, sql, params=None):
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    conn.close()
    return row


def probe_table(name):
    return (
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables"
        f" WHERE table_schema = 'public' AND table_name = '{name}')"
    )


def write_manifest(tmp_path, required_through, entries):
    path = tmp_path / "adoption_manifest.json"
    path.write_text(
        json.dumps({"required_through": required_through, "entries": entries})
    )
    return path
