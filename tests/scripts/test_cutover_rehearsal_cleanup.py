import json

import pytest

from scripts import cutover_rehearsal as rehearsal


class _Cursor:
    def __init__(self, fail_cleanup):
        self.fail_cleanup = fail_cleanup

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, statement, *args):
        if self.fail_cleanup and "DROP DATABASE" in str(statement):
            import psycopg2

            raise psycopg2.OperationalError("scratch database is busy")


class _Admin:
    autocommit = False

    def __init__(self, fail_cleanup, fail_close=False):
        self.fail_cleanup = fail_cleanup
        self.fail_close = fail_close

    def cursor(self):
        return _Cursor(self.fail_cleanup)

    def close(self):
        if self.fail_close:
            import psycopg2

            raise psycopg2.OperationalError("admin connection close failed")


def _run_with_restore_failure(tmp_path, monkeypatch, *, fail_cleanup, fail_close=False):
    dump = tmp_path / "archive.dump"
    dump.touch()
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"catalog": {"database": {"<database>": {}}}}))
    admins = [_Admin(fail_cleanup, fail_close), _Admin(fail_cleanup, fail_close)]
    monkeypatch.setattr(rehearsal.psycopg2, "connect", lambda _: admins.pop(0))
    monkeypatch.setattr(rehearsal, "require_cluster_separation", lambda *_: None)
    roles = iter((rehearsal.SERVICE_ROLES, set(), set()))
    monkeypatch.setattr(rehearsal, "_service_roles", lambda _: next(roles))
    monkeypatch.setattr(rehearsal, "_create_restore_database", lambda *_: None)
    primary = ValueError("restore stage failed")
    monkeypatch.setattr(
        rehearsal, "_restore_dump", lambda *_: (_ for _ in ()).throw(primary)
    )

    with pytest.raises(ValueError, match="restore stage failed") as caught:
        rehearsal.run(
            dump,
            "postgres://restore/admin",
            "postgres://code/admin",
            source_catalog=catalog,
            confirm_archive_backup=True,
        )
    return caught.value, primary


def test_cleanup_failure_keeps_primary_stage_error_and_chains_cleanup(
    tmp_path, monkeypatch
):
    caught, primary = _run_with_restore_failure(
        tmp_path, monkeypatch, fail_cleanup=True
    )

    assert caught is primary
    assert isinstance(caught.__cause__, RuntimeError)
    assert "scratch database is busy" in str(caught.__cause__)


def test_successful_cleanup_preserves_original_stage_error(tmp_path, monkeypatch):
    caught, primary = _run_with_restore_failure(
        tmp_path, monkeypatch, fail_cleanup=False
    )

    assert caught is primary
    assert caught.__cause__ is None


def test_admin_close_failure_keeps_primary_stage_error_and_chains_cleanup(
    tmp_path, monkeypatch
):
    caught, primary = _run_with_restore_failure(
        tmp_path, monkeypatch, fail_cleanup=False, fail_close=True
    )

    assert caught is primary
    assert isinstance(caught.__cause__, RuntimeError)
    assert "admin connection close failed" in str(caught.__cause__)
