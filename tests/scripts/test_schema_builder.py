"""The schema builder controls its namespace and rolls failed builds back."""

import pytest
from sqlalchemy import create_engine, text

from scripts.migration_runner import MIGRATIONS_DIR, adopt, apply_pending
from src.models.target import ddl
from src.models.target.ddl.__main__ import main
from tests.scripts.conftest import as_user


def _prepare_shadow(conn, schema, search_path):
    quoted = conn.dialect.identifier_preparer.quote(schema)
    conn.exec_driver_sql(f"CREATE SCHEMA {quoted}")
    conn.execute(
        text("SELECT set_config('search_path', :path, false)"), {"path": search_path}
    )
    conn.commit()


@pytest.mark.parametrize("user_schema", [False, True])
def test_build_targets_public_and_restores_the_callers_search_path(
    owner_world_db, owner_actor, user_schema
):
    owner = as_user(owner_world_db, owner_actor)
    schema = owner_actor if user_schema else "schema_builder_shadow"
    search_path = '"$user", public' if user_schema else f"{schema}, public"
    engine = create_engine(owner)
    try:
        with engine.connect() as conn:
            _prepare_shadow(conn, schema, search_path)
            ddl.build(conn)
            assert conn.execute(text("SHOW search_path")).scalar_one() == search_path
            assert (
                conn.execute(
                    text("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")
                ).scalar_one()
                == 44
            )
            assert (
                conn.execute(
                    text(
                        "SELECT count(*) FROM pg_proc p JOIN pg_namespace n"
                        " ON n.oid = p.pronamespace WHERE n.nspname = 'public'"
                    )
                ).scalar_one()
                == 85
            )
            assert (
                conn.execute(
                    text(
                        "SELECT count(*) FROM pg_class c JOIN pg_namespace n"
                        " ON n.oid = c.relnamespace WHERE n.nspname = :schema"
                    ),
                    {"schema": schema},
                ).scalar_one()
                == 0
            )
            assert (
                conn.execute(
                    text(
                        "SELECT count(*) FROM pg_proc p JOIN pg_namespace n"
                        " ON n.oid = p.pronamespace WHERE n.nspname = :schema"
                    ),
                    {"schema": schema},
                ).scalar_one()
                == 0
            )
            # Definer functions resolve their tables through public at runtime.
            assert (
                conn.execute(
                    text("SELECT count(*) FROM public.fn_health_ready_lanes()")
                ).scalar_one()
                == 0
            )
        report = adopt(owner, MIGRATIONS_DIR, MIGRATIONS_DIR / "adoption_manifest.json")
        assert [migration.version for migration in report.adopted] == [0, 1, 2]
        assert apply_pending(owner, MIGRATIONS_DIR).applied == []
        assert main(["init", "--database-url", owner]) == 0
    finally:
        engine.dispose()


def test_failed_build_restores_the_namespace_and_rolls_back_every_object(
    owner_world_db, owner_actor, monkeypatch
):
    run_file = ddl._run_file

    def fail_at_grants(conn, name):
        if name == "09_grants.sql":
            raise RuntimeError("injected grants failure")
        run_file(conn, name)

    monkeypatch.setattr(ddl, "_run_file", fail_at_grants)
    engine = create_engine(as_user(owner_world_db, owner_actor))
    try:
        with engine.connect() as conn:
            search_path = "schema_builder_shadow, public"
            _prepare_shadow(conn, "schema_builder_shadow", search_path)
            with pytest.raises(RuntimeError, match="injected grants failure"):
                ddl.build(conn)
            assert conn.execute(text("SHOW search_path")).scalar_one() == search_path
            assert (
                conn.execute(
                    text(
                        "SELECT count(*) FROM pg_tables WHERE schemaname IN ('public', 'schema_builder_shadow')"
                    )
                ).scalar_one()
                == 0
            )
            assert (
                conn.execute(
                    text("SELECT count(*) FROM pg_namespace WHERE nspname = 'archive'")
                ).scalar_one()
                == 0
            )
            assert (
                conn.execute(
                    text("SELECT count(*) FROM pg_roles WHERE rolname LIKE 'svc_%'")
                ).scalar_one()
                == 0
            )
    finally:
        engine.dispose()
