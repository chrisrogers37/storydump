"""The declared actors as test machinery (#753 PR-A, plan §0.2 actor split).

`04` §0.2: the lineage seeds under a database-owner-shaped actor
(non-superuser + CREATEROLE — so legacy tables carry *real legacy ownership*,
not the gate runner's), the step-0 bootstrap runs as that owner, and window
steps run as ``svc_migration``. These tests pin the fixtures that make every
other suite able to say "as the declared actor" and mean it.
"""

import uuid

import psycopg2
import pytest

from scripts.migration_runner import apply_pending
from tests.scripts.conftest import (
    SETUP_SQL,
    _dsn,
    as_user,
    drop_service_roles,
    fetch_one,
    psql_apply,
    window_actor,
)

pytestmark = pytest.mark.integration


class TestOwnerActor:
    def test_owner_is_nonsuperuser_createrole_login(self, owner_actor, admin_conn):
        with admin_conn.cursor() as cur:
            cur.execute(
                "SELECT rolsuper, rolcreaterole, rolcanlogin FROM pg_roles"
                " WHERE rolname = %s",
                (owner_actor,),
            )
            superuser, createrole, canlogin = cur.fetchone()
        assert superuser is False, (
            "the owner actor must be non-superuser — a superuser-only replay"
            " tests an environment production does not have"
        )
        assert createrole is True
        assert canlogin is True

    def test_owner_db_is_owned_by_the_owner(self, owner_actor, owner_db):
        row = fetch_one(
            owner_db,
            "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()",
        )
        assert row[0] == owner_actor

    def test_seed_as_owner_gives_legacy_tables_real_legacy_ownership(
        self, owner_actor, owner_db
    ):
        """The seed runs connected AS the owner (the hand-psql era, faithfully)
        — including CREATE EXTENSION "uuid-ossp", which is trusted on PG13+
        and must succeed for a non-superuser database owner. Measured here,
        not assumed."""
        psql_apply(as_user(owner_db, owner_actor), [SETUP_SQL])

        row = fetch_one(
            owner_db,
            "SELECT tableowner FROM pg_tables"
            " WHERE schemaname = 'public' AND tablename = 'posting_queue'",
        )
        assert row[0] == owner_actor, (
            "legacy tables must carry real legacy ownership, not the gate runner's"
        )
        ext = fetch_one(
            owner_db,
            "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'uuid-ossp')",
        )
        assert ext[0] is True


class TestSvcMigrationAsWindowActor:
    def test_svc_migration_connects_after_bootstrap(
        self, owner_actor, owner_window_db, admin_conn
    ):
        as_svc = window_actor(owner_window_db, owner_actor, admin_conn)

        conn = psycopg2.connect(as_svc)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT current_user")
                assert cur.fetchone()[0] == "svc_migration"
        finally:
            conn.close()

    def test_ledger_created_by_svc_migration_is_owned_by_it(
        self, owner_actor, owner_window_db, admin_conn, tmp_path
    ):
        """First runner contact as svc_migration creates the ledger — and
        must own it (production first-contact shape; bootstrap grants CREATE
        on the database)."""
        as_svc = window_actor(owner_window_db, owner_actor, admin_conn)

        apply_pending(as_svc, tmp_path)

        row = fetch_one(
            owner_window_db,
            "SELECT tableowner FROM pg_tables"
            " WHERE schemaname = 'runner' AND tablename = 'schema_migrations'",
        )
        assert row[0] == "svc_migration"


class TestSweepOwnershipGuard:
    def test_role_drop_refuses_to_touch_a_non_suite_database(
        self, owner_actor, owner_window_db, admin_conn
    ):
        """THE data-loss guard, behaviorally: the step-0 bootstrap is a
        deployment artifact, so a dev/staging database it was run against
        holds the same svc_* grants — a test sweep must refuse it by name
        and fail loudly, never drop it, live backends or not."""
        window_actor(owner_window_db, owner_actor, admin_conn)
        foreign = f"defo_not_ours_{uuid.uuid4().hex[:8]}"
        with admin_conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{foreign}"')
            cur.execute(f'GRANT CREATE ON DATABASE "{foreign}" TO svc_migration')
        try:
            with pytest.raises(psycopg2.errors.DependentObjectsStillExist):
                drop_service_roles(admin_conn)

            row = fetch_one(
                _dsn("postgres"),
                "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = %s)",
                (foreign,),
            )
            assert row[0] is True, (
                "the sweep dropped a database outside its own naming"
                " convention — the one thing it must never do"
            )
        finally:
            with admin_conn.cursor() as cur:
                cur.execute(f'REVOKE CREATE ON DATABASE "{foreign}" FROM svc_migration')
                cur.execute(f'DROP DATABASE "{foreign}"')
