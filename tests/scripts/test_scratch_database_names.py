"""Scratch database cleanup recognizes only the suite's reserved namespaces."""

import pytest

from tests.scripts.conftest import (
    _drop_db,
    _is_suite_db,
    _scratch_name,
    as_user,
    drop_service_roles,
    provision_roles,
)


@pytest.mark.parametrize(
    "name, expected",
    [
        ("runner_test_synthetic", True),
        ("runner_tpl_synthetic", True),
        ("unrelated_synthetic", False),
        ("not_runner_tpl_synthetic", False),
    ],
)
def test_cleanup_recognizes_reserved_database_names(name, expected):
    assert _is_suite_db(name) is expected


@pytest.mark.integration
def test_role_cleanup_removes_a_reserved_template_database(
    owner_world_db, owner_actor, admin_conn
):
    provision_roles(admin_conn, as_user(owner_world_db, owner_actor))
    name = _scratch_name(prefix="runner_tpl_")
    with admin_conn.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
        cur.execute(f'GRANT CREATE ON DATABASE "{name}" TO svc_migration')
    try:
        drop_service_roles(admin_conn)
        with admin_conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
            assert cur.fetchone() is None
    finally:
        _drop_db(admin_conn, name)
