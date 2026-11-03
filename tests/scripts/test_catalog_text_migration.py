"""Catalog refresh guards, adoption, and schema-code replay equivalence."""

import io
import os
import re
import subprocess
import sys
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor

import psycopg2
import pytest

from scripts.migration_runner import (
    MIGRATIONS_DIR,
    MigrationRunnerError,
    adopt,
    apply_manual,
    apply_pending,
    discover_migrations,
    split_statements,
)
from tests.scripts.conftest import (
    REPO_ROOT,
    _scratch,
    as_user,
    build_world,
    fetch_all,
    fetch_one,
    seed_workspace_chain,
    set_test_passwords,
)


def _definitions(sql):
    result = []
    for statement in split_statements(sql):
        match = re.search(
            r"(?m)^CREATE (?:OR REPLACE )?FUNCTION ([a-z_]+)\(", statement
        )
        if match:
            result.append(
                (
                    match[1],
                    statement[match.start() :].replace(
                        "CREATE OR REPLACE FUNCTION", "CREATE FUNCTION", 1
                    ),
                )
            )
    return result


def test_catalog_refresh_is_wrapped_with_three_structural_postconditions():
    migration = next(m for m in discover_migrations(MIGRATIONS_DIR) if m.version == 1)
    assert migration.execution_mode == "wrapped"
    assert len(migration.postconditions) == 3
    assert all("md5" not in probe.lower() for probe in migration.postconditions)


def test_replaced_functions_are_exact_schema_definitions():
    """Every function 001 defines is the schema's text, unless 003 replaced it
    when it renamed what that text names; every one 003 defines is too."""
    schema = _definitions(
        (REPO_ROOT / "src/models/target/ddl/04_functions.sql").read_text()
    )
    migration = _definitions((MIGRATIONS_DIR / "001_catalog_text.sql").read_text())
    rename = _definitions((MIGRATIONS_DIR / "003_instagram_past_posts.sql").read_text())
    assert len(migration) >= 16
    assert len([name for name, _ in migration if name == "fn_clock_tick"]) == 2
    replaced = {
        name.replace("archive", "past_posts")
        for name, text in migration
        if (name, text) not in schema
    }
    assert replaced <= {name for name, _ in rename}
    assert all(definition in schema for definition in rename)


@pytest.fixture(scope="module")
def schema_base_source(tmp_path_factory):
    """Export the pinned schema-code base without touching a checkout."""
    base = (REPO_ROOT / "scripts/schema_consistency_base.txt").read_text().strip()
    exported = subprocess.run(
        ["git", "archive", base], cwd=REPO_ROOT, capture_output=True
    )
    assert exported.returncode == 0, exported.stderr.decode()
    archive = exported.stdout
    source = tmp_path_factory.mktemp("schema_base")
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        bundle.extractall(source, filter="data")
    return source


@pytest.fixture
def base_world(admin_conn, owner_actor, schema_base_source):
    lifecycle = _scratch(admin_conn, owner=owner_actor, roles=[])
    database = next(lifecycle)
    owner = as_user(database, owner_actor)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "src.models.target.ddl", "build"],
            cwd=schema_base_source,
            env={**os.environ, "DATABASE_URL": owner},
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        adopt(
            owner,
            MIGRATIONS_DIR,
            MIGRATIONS_DIR / "adoption_manifest.json",
            max_version=0,
        )
        set_test_passwords(admin_conn)
        yield owner
    finally:
        lifecycle.close()


def test_new_build_adopts_catalog_refresh_and_apply_is_empty(world_db):
    rows = fetch_all(
        world_db,
        "SELECT version, status FROM runner.schema_migrations WHERE version <= 1 ORDER BY version",
    )
    assert rows == [
        {"version": 0, "status": "adopted"},
        {"version": 1, "status": "adopted"},
    ]
    assert apply_pending(world_db, MIGRATIONS_DIR).applied == []
    assert apply_pending(world_db, MIGRATIONS_DIR).applied == []


def test_predeploy_leaves_both_cutover_migrations_owed(base_world, capsys):
    """A deploy never runs 001 or 002, and the rename after them refuses to
    run until both are applied: the predeploy fails and changes nothing."""
    from scripts.migration_runner import main
    from tests.scripts.test_archive_snapshot_migration import snapshot_names

    with psycopg2.connect(base_world) as conn, conn.cursor() as cur:
        for name in snapshot_names():
            cur.execute(f'CREATE TABLE archive."{name}" (sentinel integer)')
        cur.execute("COMMENT ON FUNCTION public.fn_safe_tz(text) IS 'cutover sentinel'")

    report = apply_pending(base_world, MIGRATIONS_DIR, max_version=2)

    assert report.applied == []
    assert [migration.version for migration in report.owed] == [1, 2]
    assert fetch_one(
        base_world,
        "SELECT count(*) FROM runner.schema_migrations WHERE version IN (1, 2)",
    ) == (0,)
    assert fetch_one(
        base_world,
        "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'archive' AND c.relname LIKE '%pre_cutover%'",
    ) == (16,)
    assert fetch_one(
        base_world,
        "SELECT obj_description('public.fn_safe_tz(text)'::regprocedure, 'pg_proc')",
    ) == ("cutover sentinel",)
    assert main(["--database-url", base_world, "apply"]) == 1
    assert "003 runs after 001 and 002" in capsys.readouterr().err
    assert fetch_one(
        base_world, "SELECT count(*) FROM runner.schema_migrations WHERE version > 0"
    ) == (0,)
    assert fetch_one(
        base_world, "SELECT to_regclass('public.instagram_archive_generations')"
    ) == ("instagram_archive_generations",)


def test_fresh_schema_adopts_two_versions_before_the_archive_drop(
    owner_world_db, owner_actor
):
    from sqlalchemy import create_engine

    from src.models.target.ddl import build

    owner = as_user(owner_world_db, owner_actor)
    engine = create_engine(owner)
    try:
        with engine.connect() as conn:
            build(conn)
        report = adopt(
            owner,
            MIGRATIONS_DIR,
            MIGRATIONS_DIR / "adoption_manifest.json",
            max_version=1,
        )
        assert [migration.version for migration in report.adopted] == [0, 1]
        assert apply_pending(owner, MIGRATIONS_DIR, max_version=1).applied == []
        assert apply_pending(owner, MIGRATIONS_DIR, max_version=1).applied == []
    finally:
        engine.dispose()


@pytest.mark.parametrize("guard", ["legacy_queue_item_id", "legacy_backfill"])
def test_catalog_refresh_guard_rolls_back_every_change(base_world, guard):
    from scripts.schema_parity import schema_diff, schema_signature

    with psycopg2.connect(base_world) as conn:
        chain = seed_workspace_chain(conn, "guard-example")
        with conn.cursor() as cur:
            if guard == "legacy_queue_item_id":
                cur.execute(
                    "UPDATE post_intents SET legacy_queue_item_id = gen_random_uuid() WHERE id = %s",
                    (chain["intent"],),
                )
            else:
                cur.execute(
                    "UPDATE post_intents SET published_via = 'legacy_backfill' WHERE id = %s",
                    (chain["intent"],),
                )
            cur.execute(
                "COMMENT ON FUNCTION public.fn_safe_tz(text) IS 'guard sentinel'"
            )
    before = fetch_one(
        base_world,
        "SELECT obj_description('public.fn_safe_tz(text)'::regprocedure, 'pg_proc')",
    )[0]
    before_schema = schema_signature(base_world)
    with pytest.raises(MigrationRunnerError, match=guard):
        apply_manual(base_world, MIGRATIONS_DIR, 1)
    assert (
        fetch_one(
            base_world,
            "SELECT obj_description('public.fn_safe_tz(text)'::regprocedure, 'pg_proc')",
        )[0]
        == before
    )
    assert fetch_one(
        base_world, "SELECT count(*) FROM runner.schema_migrations WHERE version = 1"
    ) == (0,)
    assert fetch_one(
        base_world,
        "SELECT count(*) FROM pg_attribute WHERE attrelid = 'public.post_intents'::regclass AND attname = 'legacy_queue_item_id' AND NOT attisdropped",
    ) == (1,)
    assert schema_diff(before_schema, schema_signature(base_world)) == []


def test_catalog_refresh_targets_public_with_an_alternate_search_path(base_world):
    from psycopg2.extensions import make_dsn

    from scripts.migration_runner import _apply_one

    grants_query = (
        "SELECT p.oid, p.proowner, p.proacl::text AS acl FROM pg_proc p"
        " JOIN pg_namespace n ON n.oid = p.pronamespace"
        " WHERE n.nspname = 'public' ORDER BY p.oid"
    )
    before_grants = fetch_all(base_world, grants_query)
    migration = next(m for m in discover_migrations(MIGRATIONS_DIR) if m.version == 1)
    connection = psycopg2.connect(
        make_dsn(base_world, options="-csearch_path=catalog_refresh_shadow,public")
    )
    connection.autocommit = True
    try:
        with connection.cursor() as cur:
            cur.execute("CREATE SCHEMA catalog_refresh_shadow")
            cur.execute("SHOW search_path")
            original_path = cur.fetchone()[0]
        _apply_one(connection, migration)
        with connection.cursor() as cur:
            cur.execute("SHOW search_path")
            assert cur.fetchone()[0] == original_path
        assert fetch_all(base_world, grants_query) == before_grants
        assert fetch_one(
            base_world,
            "SELECT count(*) FROM pg_proc p JOIN pg_namespace n"
            " ON n.oid = p.pronamespace WHERE n.nspname = 'catalog_refresh_shadow'",
        ) == (0,)
        assert fetch_one(
            base_world,
            "SELECT prosrc LIKE '%rate_counters%' AND prosrc NOT LIKE '%jobs_ok%'"
            " FROM pg_proc WHERE oid ="
            " 'public.fn_retention_batch(text,interval,integer)'::regprocedure",
        ) == (True,)
        assert fetch_one(
            base_world,
            "SELECT status FROM runner.schema_migrations WHERE version = 1",
        ) == ("applied",)
    finally:
        connection.close()


def test_partial_schema_does_not_adopt_catalog_refresh(base_world):
    report = adopt(
        base_world,
        MIGRATIONS_DIR,
        MIGRATIONS_DIR / "adoption_manifest.json",
        max_version=1,
    )
    assert [migration.version for migration in report.pending] == [1]
    assert report.adopted == []


def test_catalog_checks_are_validated_in_a_separate_transaction(base_world):
    validation_query = (
        "SELECT conname, convalidated FROM pg_constraint"
        " WHERE conrelid = 'public.post_intents'::regclass"
        " AND conname IN ('ck_intent_via', 'ck_posted_complete') ORDER BY conname"
    )
    apply_manual(base_world, MIGRATIONS_DIR, 1)

    assert fetch_all(base_world, validation_query) == [
        {"conname": "ck_intent_via", "convalidated": False},
        {"conname": "ck_posted_complete", "convalidated": False},
    ]
    with psycopg2.connect(base_world) as conn, conn.cursor() as cur:
        cur.execute(
            (REPO_ROOT / "scripts/validate_catalog_constraints.sql").read_text()
        )
    assert fetch_all(base_world, validation_query) == [
        {"conname": "ck_intent_via", "convalidated": True},
        {"conname": "ck_posted_complete", "convalidated": True},
    ]


def test_guard_observes_a_writer_that_commits_while_the_migration_waits(base_world):
    writer = psycopg2.connect(base_world)
    try:
        chain = seed_workspace_chain(writer, "concurrent-guard-example")
        with writer.cursor() as cur:
            cur.execute(
                "UPDATE post_intents SET legacy_queue_item_id = gen_random_uuid() WHERE id = %s",
                (chain["intent"],),
            )
        with ThreadPoolExecutor(max_workers=1) as pool:
            migration = pool.submit(apply_manual, base_world, MIGRATIONS_DIR, 1)
            deadline = time.monotonic() + 10
            try:
                while not fetch_one(
                    base_world,
                    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE database ="
                    " (SELECT oid FROM pg_database WHERE datname = current_database())"
                    " AND relation = 'public.post_intents'::regclass"
                    " AND mode = 'AccessExclusiveLock' AND NOT granted)",
                )[0]:
                    assert not migration.done(), (
                        "migration does not wait for the writer"
                    )
                    assert time.monotonic() < deadline, (
                        "migration never waits on the table"
                    )
                    time.sleep(0.01)
                writer.commit()
                with pytest.raises(MigrationRunnerError, match="legacy_queue_item_id"):
                    migration.result(timeout=10)
            finally:
                writer.rollback()
        assert fetch_one(
            base_world,
            "SELECT legacy_queue_item_id IS NOT NULL FROM post_intents WHERE id = %s",
            (chain["intent"],),
        ) == (True,)
        assert fetch_one(
            base_world,
            "SELECT count(*) FROM runner.schema_migrations WHERE version = 1",
        ) == (0,)
    finally:
        writer.close()


@pytest.mark.parametrize(
    "retention_class",
    [
        "audit_events",
        "jobs_ok",
        "jobs_bad",
        "provider_operations",
        "channel_outbox",
        "daily_post_counts",
    ],
)
def test_retention_refuses_classes_the_worker_does_not_call(world_db, retention_class):
    with psycopg2.connect(as_user(world_db, "svc_worker")) as conn:
        with conn.cursor() as cur:
            with pytest.raises(psycopg2.Error, match="unknown retention class"):
                cur.execute(
                    "SELECT fn_retention_batch(%s, interval '90 days', 5)",
                    (retention_class,),
                )


def _through_002(dsn):
    """The base as production stood before 003: 001, its validation, then 002."""
    assert [m.version for m in apply_manual(dsn, MIGRATIONS_DIR, 1).applied] == [1]
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            (REPO_ROOT / "scripts/validate_catalog_constraints.sql").read_text()
        )
    report = apply_manual(dsn, MIGRATIONS_DIR, 2, confirm_archive_backup=True)
    assert [m.version for m in report.applied] == [2]


def test_the_rename_refuses_where_past_posts_data_exists(base_world):
    """A stored item's identity and its tombstones name the provider, so the
    rename runs only where no Instagram source exists yet; with one it changes
    nothing."""
    _through_002(base_world)
    with psycopg2.connect(base_world) as conn:
        chain = seed_workspace_chain(conn, "rename-guard-example")
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO media_sources (workspace_id, provider, config,"
                " origin_ig_account_id) VALUES (%s, 'instagram_archive', '{}', %s)",
                (chain["ws"], chain["iga"]),
            )
        conn.commit()

    with pytest.raises(MigrationRunnerError, match="past-posts data exists"):
        apply_pending(base_world, MIGRATIONS_DIR)

    assert fetch_one(
        base_world, "SELECT count(*) FROM runner.schema_migrations WHERE version = 3"
    ) == (0,)
    assert fetch_one(
        base_world,
        "SELECT count(*) FROM media_sources WHERE provider = 'instagram_archive'",
    ) == (1,)


def test_the_rename_carries_stored_jobs_to_the_new_kinds(base_world):
    _through_002(base_world)
    with psycopg2.connect(base_world) as conn:
        chain = seed_workspace_chain(conn, "rename-jobs-example")
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO jobs (kind, workspace_id, lane, serialization_key,"
                " max_attempts) VALUES"
                " ('archive_lifecycle_sweep', NULL, 'bulk', 'archive_lifecycle_sweep', 1),"
                " ('reconcile_archive_accounts', %s, 'bulk', 'archive-reconcile:' || %s, 5)",
                (chain["ws"], str(chain["ws"])),
            )
        conn.commit()

    assert [m.version for m in apply_pending(base_world, MIGRATIONS_DIR).applied] == [
        3,
        4,
        5,
        8,
    ]

    assert fetch_all(
        base_world,
        "SELECT kind, serialization_key = 'past-posts-reconcile:' || workspace_id::text"
        " OR serialization_key = kind AS key_follows FROM jobs"
        " WHERE kind LIKE '%past_posts%' ORDER BY kind",
    ) == [
        {"kind": "past_posts_lifecycle_sweep", "key_follows": True},
        {"kind": "reconcile_past_posts_accounts", "key_follows": True},
    ]


def test_base_with_catalog_refresh_equals_current_code(
    base_world, admin_conn, owner_actor
):
    """The base with the whole tail, in version order, is the current code."""
    from scripts.schema_parity import schema_diff, schema_signature

    _through_002(base_world)
    assert [m.version for m in apply_pending(base_world, MIGRATIONS_DIR).applied] == [
        3,
        4,
        5,
        8,
    ]
    lifecycle = _scratch(admin_conn, owner=owner_actor)
    database = next(lifecycle)
    try:
        expected = build_world(database, owner_actor, admin_conn)
        assert (
            schema_diff(schema_signature(base_world), schema_signature(expected)) == []
        )
    finally:
        lifecycle.close()
