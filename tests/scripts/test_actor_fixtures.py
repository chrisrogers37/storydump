"""The owner and runtime actors used by schema-backed tests."""

import uuid

import psycopg2
import pytest
from sqlalchemy import create_engine

from src.models.target.ddl import NotEmpty, build
from tests.scripts.conftest import (
    SERVICE_ROLES,
    _dsn,
    as_user,
    build_world,
    drop_service_roles,
    fetch_one,
    maintenance_conn,
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
        assert superuser is False
        assert createrole is True
        assert canlogin is True

    def test_owner_db_is_owned_by_the_owner(self, owner_actor, owner_db):
        row = fetch_one(
            owner_db,
            "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()",
        )
        assert row[0] == owner_actor


class TestBuiltWorld:
    def test_schema_and_ledger_are_owned_by_the_owner(self, world_db, owner_actor):
        for schema, table in (
            ("public", "workspaces"),
            ("runner", "schema_migrations"),
        ):
            row = fetch_one(
                world_db,
                "SELECT tableowner FROM pg_tables WHERE schemaname = %s AND tablename = %s",
                (schema, table),
            )
            assert row == (owner_actor,)
        assert fetch_one(
            world_db,
            "SELECT version, status FROM runner.schema_migrations ORDER BY version",
        ) == (0, "adopted")

    @pytest.mark.parametrize("login", ["svc_ingress", "svc_worker", "svc_migration"])
    def test_service_login_connects_after_build(self, world_db, login):
        assert fetch_one(as_user(world_db, login), "SELECT current_user") == (login,)

    def test_build_refuses_a_built_world_and_leaves_it_unchanged(self, world_db):
        before = fetch_one(
            world_db, "SELECT count(*) FROM public.post_intent_transitions"
        )
        engine = create_engine(world_db)
        try:
            with engine.connect() as conn, pytest.raises(NotEmpty):
                build(conn)
        finally:
            engine.dispose()
        assert (
            fetch_one(world_db, "SELECT count(*) FROM public.post_intent_transitions")
            == before
        )

    def test_builder_refuses_a_connection_outside_the_cluster_lock(
        self, owner_world_db, owner_actor
    ):
        outsider = maintenance_conn()
        try:
            with pytest.raises(
                RuntimeError, match="does not hold SUITE_CLUSTER_LOCK_KEY"
            ):
                build_world(owner_world_db, owner_actor, outsider)
        finally:
            outsider.close()
        assert fetch_one(
            owner_world_db,
            "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'",
        ) == (0,)

    def test_service_roles_are_bracketed(self, owner_world_db, owner_actor, admin_conn):
        assert fetch_one(
            owner_world_db,
            "SELECT count(*) FROM pg_roles WHERE rolname = ANY(%s)",
            (list(SERVICE_ROLES),),
        ) == (0,)
        build_world(owner_world_db, owner_actor, admin_conn)
        assert fetch_one(
            owner_world_db,
            "SELECT count(*) FROM pg_roles WHERE rolname = ANY(%s)",
            (list(SERVICE_ROLES),),
        ) == (len(SERVICE_ROLES),)


class TestSweepOwnershipGuard:
    def test_role_drop_refuses_to_touch_a_non_suite_database(
        self, world_db, admin_conn
    ):
        """Role cleanup never drops databases outside the suite's namespace."""
        foreign = f"defo_not_ours_{uuid.uuid4().hex[:8]}"
        with admin_conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{foreign}"')
            cur.execute(f'GRANT CREATE ON DATABASE "{foreign}" TO svc_migration')
        try:
            with pytest.raises(psycopg2.errors.DependentObjectsStillExist):
                drop_service_roles(admin_conn)
            assert fetch_one(
                _dsn("postgres"),
                "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = %s)",
                (foreign,),
            ) == (True,)
        finally:
            with admin_conn.cursor() as cur:
                cur.execute(f'REVOKE CREATE ON DATABASE "{foreign}" FROM svc_migration')
                cur.execute(f'DROP DATABASE "{foreign}"')
