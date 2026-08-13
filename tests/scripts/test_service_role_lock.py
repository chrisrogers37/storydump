"""The service-role lock (#758 part 3, #768).

#763 gave every test session its own database, which removed the collision on
the database name. It could not remove the collision on the **roles**: they are
CLUSTER-scoped, so a unique database name does not isolate them and the seven
fixed-name `svc_*` roles are shared by every session on the host.

The failure that leaves is the mirror of the one #758 part 2 fixed. Part 2 was a
false PASS — tests that never ran, reported green. This is a false FAIL —
`roleless_db`'s **setup** drops all seven roles cluster-wide, unconditionally,
so a second session tears the roles out from under a first one that is actively
using them, and each `DROP ROLE` can equally fail with
`DependentObjectsStillExist` because the other session's database still holds
grants to them. Measured (#768): two concurrent runs of the role suites both
return rc=1, while the same suite alone is green.

These tests need no PostgreSQL. That is deliberate: the lock is the mechanism
that has to hold when the database layer is already contended, so its own proof
should not depend on the contended thing.
"""

import multiprocessing
import os
import time

import pytest

from tests.scripts.conftest import (
    service_role_lock,
    service_role_lock_path,
)


class TestTheLockIsKeyedToTheCluster:
    """Roles are cluster-scoped, so the CLUSTER is the correct scope for the
    lock — not the repo, not the checkout, and not one hardcoded path."""

    def test_the_path_is_derived_from_host_and_port(self, monkeypatch):
        from src.config.settings import settings

        monkeypatch.setattr(settings, "DB_HOST", "localhost")
        monkeypatch.setattr(settings, "DB_PORT", 5432)
        first = service_role_lock_path()

        monkeypatch.setattr(settings, "DB_PORT", 5433)
        second = service_role_lock_path()

        assert first != second, (
            "two different clusters share a lock file — they would serialise"
            " against each other for no reason"
        )

    def test_the_same_cluster_resolves_to_the_same_path(self, monkeypatch):
        """The half that actually does the work: two checkouts pointing at one
        PostgreSQL must land on the same file, or the lock guards nothing."""
        from src.config.settings import settings

        monkeypatch.setattr(settings, "DB_HOST", "db.example")
        monkeypatch.setattr(settings, "DB_PORT", 5432)
        first = service_role_lock_path()
        second = service_role_lock_path()

        assert first == second

    def test_the_name_is_readable_rather_than_hashed(self, monkeypatch):
        """An operator who finds this file in the temp dir should be able to
        tell what it guards and which cluster it belongs to."""
        from src.config.settings import settings

        monkeypatch.setattr(settings, "DB_HOST", "localhost")
        monkeypatch.setattr(settings, "DB_PORT", 5432)
        name = service_role_lock_path().name

        assert "svc-roles" in name
        assert "localhost" in name and "5432" in name


def _hold_lock(started, release):
    """Acquire in a separate PROCESS and hold until told to stop."""
    with service_role_lock():
        started.set()
        release.wait(timeout=30)


class TestItActuallyExcludes:
    """A lock that has only ever been acquired uncontended is not known to
    exclude anything."""

    def test_a_second_holder_is_blocked_while_the_first_holds(self):
        ctx = multiprocessing.get_context("fork")
        started, release = ctx.Event(), ctx.Event()
        holder = ctx.Process(target=_hold_lock, args=(started, release))
        holder.start()
        try:
            assert started.wait(timeout=15), "the holder never acquired"

            # Now try to take it here, with a short bound. It must NOT succeed.
            os.environ["SVC_ROLE_LOCK_TIMEOUT_S"] = "1"
            import importlib

            import tests.scripts.conftest as cf

            importlib.reload(cf)
            began = time.monotonic()
            with pytest.raises(RuntimeError, match="service-role lock"):
                with cf.service_role_lock():
                    pass
            waited = time.monotonic() - began

            assert waited >= 1.0, f"gave up in {waited:.2f}s without waiting"
        finally:
            release.set()
            holder.join(timeout=15)
            os.environ.pop("SVC_ROLE_LOCK_TIMEOUT_S", None)
            import importlib

            import tests.scripts.conftest as cf

            importlib.reload(cf)

    def test_it_is_acquirable_once_the_holder_exits(self):
        """The crash-safety property, and the reason this is `flock` rather
        than a lock file with hand-rolled cleanup: the kernel releases it when
        the holding PROCESS dies, so a killed session cannot wedge the host.
        Asserted by killing the holder outright rather than letting it exit."""
        ctx = multiprocessing.get_context("fork")
        started, release = ctx.Event(), ctx.Event()
        holder = ctx.Process(target=_hold_lock, args=(started, release))
        holder.start()
        assert started.wait(timeout=15), "the holder never acquired"

        holder.kill()
        holder.join(timeout=15)

        # No release ever ran. The kernel must have dropped it anyway.
        with service_role_lock():
            pass
