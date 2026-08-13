"""The suite cluster lock (#758 part 3, #768, consolidated in #785).

#763 gave every test session its own database, which removed the collision on
the database name. It could not remove the collision on the **roles**: they are
CLUSTER-scoped, so a unique database name does not isolate them and the seven
fixed-name `svc_*` roles are shared by every session reaching this cluster.

The failure that leaves is the mirror of the one #758 part 2 fixed. Part 2 was a
false PASS — tests that never ran, reported green. This is a false FAIL —
`roleless_db`'s **setup** drops all seven roles cluster-wide, unconditionally,
so a second session tears the roles out from under a first one that is actively
using them, and each `DROP ROLE` can equally fail with
`DependentObjectsStillExist` because the other session's database still holds
grants to them. Measured (#768): two concurrent runs of the role suites both
return rc=1, while the same suite alone is green.

**Why these tests now need PostgreSQL, when their predecessors did not.** The
`flock` these replace was provable without a database, and that was a real
property — argued at the time as "the lock has to hold when the database layer
is already contended, so its own proof should not depend on the contended
thing". #785 gave it up deliberately. A file lock is HOST-scoped and the
resource is CLUSTER-scoped, so the well-tested mechanism was testing the wrong
scope; contended is also not the same as unavailable, and every other test in
this directory already requires the cluster. The cost is stated rather than
hidden: three path-shape tests died with the file lock, and the two property
tests below — exclusion and crash safety — are what actually carried the
guarantee.
"""

import multiprocessing
import os
import time

import psycopg2
import pytest

from tests.scripts.conftest import (
    SUITE_CLUSTER_LOCK_KEY,
    _dsn,
)

#: A key this suite never uses for real, so contention tests cannot be confused
#: by the session's own lock — or corrupt it.
SCRATCH_KEY = 785_2026


def _maintenance_conn():
    conn = psycopg2.connect(_dsn("postgres"))
    conn.autocommit = True
    return conn


def _try_take(key: int) -> bool:
    """Can a connection that shares nothing with this session take `key`?"""
    conn = _maintenance_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (key,))
            got = cur.fetchone()[0]
            if got:
                cur.execute("SELECT pg_advisory_unlock(%s)", (key,))
            return got
    finally:
        conn.close()


def _hold_scratch_key(started, release, tmpdir=None):
    """Acquire SCRATCH_KEY in a separate PROCESS and hold until told to stop."""
    if tmpdir is not None:
        os.environ["TMPDIR"] = tmpdir
    conn = _maintenance_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s)", (SCRATCH_KEY,))
        started.set()
        release.wait(timeout=30)


class TestTheSessionActuallyHoldsIt:
    """With the redundant `flock` gone (#785), this advisory lock is the ONLY
    mutex left. If the acquisition in `admin_conn` were ever dropped, nothing
    would serialise and #768 would come back — silently, because an unlocked
    run is green until it happens to overlap another one."""

    def test_admin_conn_holds_the_suite_lock(self, admin_conn):
        """THE REGRESSION GATE for the consolidation."""
        assert _try_take(SUITE_CLUSTER_LOCK_KEY) is False, (
            "an outside connection took the suite lock while a session was"
            " running — the session is not holding it, so nothing serialises"
        )

    def test_the_hold_is_visible_and_attributable(self, admin_conn):
        """The wait loop prints who holds it. That only works if the lock is
        discoverable in `pg_locks` — the property a file lock cannot offer,
        whose own timeout message had to tell operators to go hunting."""
        conn = _maintenance_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT a.pid, a.usename FROM pg_locks l"
                    " JOIN pg_stat_activity a ON a.pid = l.pid"
                    " WHERE l.locktype = 'advisory' AND l.objid = %s"
                    "   AND l.granted",
                    (SUITE_CLUSTER_LOCK_KEY,),
                )
                holder = cur.fetchone()
        finally:
            conn.close()

        assert holder is not None, "the lock is held but names no holder"
        assert holder[0] and holder[1]


class TestItActuallyExcludes:
    """A lock that has only ever been acquired uncontended is not known to
    exclude anything."""

    def test_a_second_holder_is_blocked_while_the_first_holds(self):
        ctx = multiprocessing.get_context("fork")
        started, release = ctx.Event(), ctx.Event()
        holder = ctx.Process(target=_hold_scratch_key, args=(started, release))
        holder.start()
        try:
            assert started.wait(timeout=15), "the holder never acquired"

            assert _try_take(SCRATCH_KEY) is False
        finally:
            release.set()
            holder.join(timeout=15)

        assert _try_take(SCRATCH_KEY) is True, "not released when the holder exited"

    def test_it_reaches_a_process_sharing_nothing_but_the_cluster(self):
        """WHY THIS LOCK AND NOT A FILE LOCK (#785).

        The holder runs with a different ``TMPDIR``, which is enough to send
        `tempfile.gettempdir()` somewhere else — so the `flock` this replaces
        would have been taken on a DIFFERENT FILE and would not have excluded
        anything. The two processes still contend here, because the mutex lives
        in the cluster rather than on the filesystem.

        On this fleet every bot sets ``TMPDIR=/tmp``, so that divergence was
        latent rather than live. The scope mismatch it stands for is not: two
        containers or hosts against one cluster share no temp directory at all.
        """
        elsewhere = "/tmp/storydump-785-probe"
        os.makedirs(elsewhere, exist_ok=True)

        ctx = multiprocessing.get_context("fork")
        started, release = ctx.Event(), ctx.Event()
        holder = ctx.Process(
            target=_hold_scratch_key, args=(started, release, elsewhere)
        )
        holder.start()
        try:
            assert started.wait(timeout=15), "the holder never acquired"

            assert _try_take(SCRATCH_KEY) is False, (
                "a process with a different TMPDIR did not contend — the mutex"
                " is not cluster-scoped"
            )
        finally:
            release.set()
            holder.join(timeout=15)
            os.rmdir(elsewhere)

    def test_it_clears_when_the_holder_is_killed(self):
        """Crash safety, measured rather than assumed — and the reason the
        `flock` had no advantage worth widening. No release path can run: the
        holder is killed outright. PostgreSQL sees the connection drop and
        releases the session lock with it.
        """
        ctx = multiprocessing.get_context("fork")
        started, release = ctx.Event(), ctx.Event()
        holder = ctx.Process(target=_hold_scratch_key, args=(started, release))
        holder.start()
        assert started.wait(timeout=15), "the holder never acquired"
        assert _try_take(SCRATCH_KEY) is False, "precondition: the key is held"

        holder.kill()
        holder.join(timeout=15)

        deadline = 15
        for _ in range(deadline * 10):
            if _try_take(SCRATCH_KEY):
                return

            time.sleep(0.1)
        pytest.fail(f"the lock was still held {deadline}s after the holder was killed")
