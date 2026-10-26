"""The schema builder controls its namespace and rolls failed builds back, and
refuses a URL it cannot read by the variable's name."""

import pytest
from sqlalchemy import Engine, create_engine, exc as sa_exc, text

from scripts.migration_runner import MIGRATIONS_DIR, adopt, apply_pending
from src.models.target import ddl
from src.models.target.ddl.__main__ import main, run
from tests.scripts.conftest import as_user
from tests.unreadable_urls import (
    BUILDER_READABLE,
    BUILDER_UNREADABLE,
    MISREAD_HOSTS,
    OWNER_URL,
    REFUSAL,
    assert_names_only_the_host,
    assert_refused_by_name,
    assert_withheld,
    cases,
    forms,
)


@pytest.mark.parametrize("url", **cases(BUILDER_UNREADABLE))
def test_an_unreadable_url_is_refused_by_name(url, capsys):
    """SQLAlchemy quotes the part of a URL it could not read, such as a port
    read out of the password, and libpq would quote a host or a database name
    read out of it, so the builder names the variable instead and exits 2, as
    it does without one, before it reads its command. No database: the URL is
    refused before anything connects."""
    assert main(["init", "--database-url", url]) == 2
    out, err = capsys.readouterr()
    assert f"refused: {OWNER_URL} {REFUSAL}" in err
    assert_withheld(out, err)

    with pytest.raises(ValueError) as caught:
        run("build", url)
    assert_refused_by_name(caught.value, OWNER_URL)


@pytest.mark.parametrize("url", **cases(BUILDER_READABLE))
def test_control_a_url_it_reads_fails_on_the_host_alone(url):
    """A URL the builder reads is not refused: connecting fails on a host
    that never resolves, and that error names the host, never the password."""
    with pytest.raises(sa_exc.OperationalError) as caught:
        main(["build", "--database-url", url])
    assert_names_only_the_host(str(caught.value))


@pytest.mark.parametrize("url", **cases(MISREAD_HOSTS))
def test_a_host_read_out_of_the_password_is_refused_before_connecting(url, monkeypatch):
    """libpq would resolve the host SQLAlchemy read, sending the password's
    tail to the resolver, and a name that resolved, under a wildcard record,
    would fail with an error that quotes it. The refusal comes first."""

    def connected(_engine):
        raise AssertionError("the builder connected to a host read out of the password")

    monkeypatch.setattr(Engine, "connect", connected)
    with pytest.raises(ValueError) as caught:
        run("build", url)
    assert_refused_by_name(caught.value, OWNER_URL)


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
                == 86
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
        assert [migration.version for migration in report.adopted] == [
            0,
            1,
            2,
            3,
            4,
            5,
            6,
        ]
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


@pytest.mark.parametrize(
    "url",
    **cases(
        forms(
            "raw-at-sign-in-a-socket-directory-and-no-user",
            "encoded-socket-directory-beside-a-password",
            "encoded-abstract-socket-beside-a-password",
        )
    ),
)
def test_control_an_at_sign_in_a_host_value_goes_on_to_connect(url, monkeypatch):
    """A socket directory or an abstract socket named in a `host` query value
    goes on to connect when its `@` is the URL's only one, or is encoded
    beside a user's: the builder reads the URL and connects."""

    class Connected(Exception):
        pass

    def connected(_engine):
        raise Connected

    monkeypatch.setattr(Engine, "connect", connected)
    with pytest.raises(Connected):
        run("build", url)
