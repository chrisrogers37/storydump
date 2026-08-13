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
import time

import pytest

from tests.scripts.conftest import (
    SCRATCH_LOCK_KEY,
    SUITE_CLUSTER_LOCK_KEY,
    lock_holder,
    maintenance_conn,
)


def _try_take(key: int) -> bool:
    """Can a connection that shares nothing with this session take `key`?

    A SEPARATE connection is required rather than incidental: advisory locks
    are re-entrant within a session, so asking `admin_conn` whether the suite
    lock is free returns True and every exclusion assertion below would be
    vacuous.
    """
    conn = maintenance_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (key,))
            got = cur.fetchone()[0]
            if got:
                cur.execute("SELECT pg_advisory_unlock(%s)", (key,))
            return got
    finally:
        conn.close()


def _hold_scratch_key(started, release):
    """Acquire SCRATCH_LOCK_KEY in a separate PROCESS and hold until told."""
    conn = maintenance_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s)", (SCRATCH_LOCK_KEY,))
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
        """The wait loop prints who holds it — the property a file lock cannot
        offer, whose own timeout message had to send operators hunting instead.

        Asserted through `lock_holder`, the function the wait loop actually
        calls, rather than a second copy of the query. A re-implementation here
        would assert that PostgreSQL has a row: the real query could break and
        every wait could print `held by None` with this still green.
        """
        conn = maintenance_conn()
        try:
            with conn.cursor() as cur:
                holder = lock_holder(cur, SUITE_CLUSTER_LOCK_KEY)
        finally:
            conn.close()

        assert holder is not None, "the lock is held but names no holder"
        pid, usename, _application = holder
        assert pid and usename


class TestItActuallyExcludes:
    """A lock that has only ever been acquired uncontended is not known to
    exclude anything."""

    def test_a_second_holder_is_blocked_while_the_first_holds(self):
        """A separate PROCESS, which is the shape that matters: the sessions
        this serialises are other pytest runs, and they share nothing with this
        one except the cluster the mutex lives in.
        """
        ctx = multiprocessing.get_context("fork")
        started, release = ctx.Event(), ctx.Event()
        holder = ctx.Process(target=_hold_scratch_key, args=(started, release))
        holder.start()
        try:
            assert started.wait(timeout=15), "the holder never acquired"

            assert _try_take(SCRATCH_LOCK_KEY) is False
        finally:
            release.set()
            holder.join(timeout=15)

        assert _try_take(SCRATCH_LOCK_KEY) is True, (
            "not released when the holder exited"
        )

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
        assert _try_take(SCRATCH_LOCK_KEY) is False, "precondition: the key is held"

        holder.kill()
        holder.join(timeout=15)

        # Bounded by the CLOCK, not by an iteration count: each probe opens a
        # connection (~50ms measured), so `range(150)` would have overrun the
        # 15s its own failure message claims. The release is asynchronous —
        # `join()` returning means the process is reaped, not that the backend
        # has noticed the socket close — so a bound is needed either way.
        budget_s = 15.0
        end = time.monotonic() + budget_s
        while time.monotonic() < end:
            if _try_take(SCRATCH_LOCK_KEY):
                return
            time.sleep(0.1)
        pytest.fail(f"still held {budget_s:.0f}s after the holder was killed")
