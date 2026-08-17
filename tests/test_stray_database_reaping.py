"""Crashed runs' leftover databases are reaped safely, or not at all (#758).

#763 gave every session a uniquely-named database, which closed the collision
half of this issue. It did not close the other half: a run that dies never drops
its database, nothing else reaps it, and six such strays were sitting on the
shared cluster when this was written. `tests/scripts/_sweep_leftovers` cannot
help — it is scoped to that suite's own `runner_test_`/`runner_tpl_` convention,
correctly, and every one of those six tests False against it.

**The obvious fix is a trap, and these tests exist to keep it out.** Copying
that sweep's rule — drop a suite-prefixed database with zero live backends —
recreates this issue's original symptom in a form that gets blamed on the
victim. Measured over two full live sessions, sampling `pg_stat_activity` every
200 ms against the session's own database: **12.2%** and **8.7%** of samples had
zero backends, with a contiguous zero window of ~**2.6 s**. A test database is
idle for long stretches, because the unit tests never touch it and SQLAlchemy
returns pooled connections. So a backend-count reaper drops a peer's live
database at roughly 1-in-10 per sweep, and that peer sees connection-loss errors
in files they never touched.

That sweep is safe anyway, and the reason is worth being precise about: it holds
`SUITE_CLUSTER_LOCK_KEY` for its whole session, so no concurrent session of that
suite can exist while it runs. **Its predicate is incidentally correct; its mutex
is what guarantees it.** Copying the predicate to a suite with no such mutex
moves the check without the guarantee.

So ownership is signalled explicitly, by a session-held advisory lock keyed on
the database name, and these tests pin the two properties that makes or breaks:
a held lock protects an idle database, and a released one stops protecting it.
"""

import hashlib
import uuid

import psycopg2
import pytest

from src.config.settings import settings
from tests.conftest import (
    MAINTENANCE_DB,
    REAP_ARMED_ENV,
    TEST_DB_BASE_NAME,
    assert_maintenance_scoped,
    connection_to,
    is_own_convention,
    maintenance_connection,
    precondition_absent,
    reap_stray_databases,
    reaping_is_armed,
    require_role_privilege,
    scan_and_reap_strays,
    session_database_lock_key,
    unowned_strays,
)


def _stray_name() -> str:
    """A name of exactly the shape #763 generates, so it is a real candidate."""
    return f"{TEST_DB_BASE_NAME}_{uuid.uuid4().hex[:10]}"


@pytest.fixture
def cluster():
    """A maintenance connection, or a named skip/failure if none is reachable."""
    try:
        conn = maintenance_connection()
    except psycopg2.OperationalError as exc:
        precondition_absent(
            f"no PostgreSQL answered at {settings.DB_HOST}:{settings.DB_PORT} — {exc}"
        )
    try:
        require_role_privilege(conn, "rolcreatedb", "CREATEDB")
        yield conn
    finally:
        conn.close()


@pytest.fixture
def a_stray_database(cluster):
    """A database of this suite's convention that nobody is holding.

    Named to match, because a candidate that the ownership predicate rejects
    would make every assertion below vacuously true — the fixture would prove
    the reaper ignores it for the wrong reason.
    """
    name = _stray_name()
    with cluster.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    try:
        yield name
    finally:
        with cluster.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}"')


class TestTheOwnershipBoundary:
    """Which databases this suite is entitled to touch at all. Everything below
    it is a sweep deciding what it may destroy, so near-misses matter more than
    the happy case."""

    def test_the_shape_this_suite_generates_is_ours(self):
        assert is_own_convention(f"{TEST_DB_BASE_NAME}_0123456789") is True
        assert is_own_convention(f"{TEST_DB_BASE_NAME}_abcdef0123") is True

    @pytest.mark.parametrize(
        "name",
        [
            "",
            "postgres",
            "storyline_ai",
            "runner_test_0123456789",
            "runner_tpl_0123456789",
        ],
    )
    def test_a_foreign_database_is_never_ours(self, name):
        assert is_own_convention(name) is False

    @pytest.mark.parametrize(
        "suffix",
        [
            "",  # the bare configured base — someone's actual database
            "_snapshot",  # a hand-made neighbour a LIKE prefix would match
            "_012345678",  # nine hex, one short
            "_01234567890",  # eleven hex, one long
            "_ABCDEF0123",  # uppercase: not what uuid4().hex produces
            "_0123456789_x",  # our shape plus a tail
            "ing_0123456789",  # base is a PREFIX of another base
        ],
    )
    def test_near_misses_are_not_ours(self, suffix):
        """THE REASON THIS IS ANCHORED RATHER THAN A PREFIX TEST. Every name
        here is matched by `LIKE 'storyline_test%'`, and none of them is a
        database this suite created. A prefix predicate would hand all seven to
        the reaper."""
        assert is_own_convention(f"{TEST_DB_BASE_NAME}{suffix}") is False

    def test_the_predicate_follows_the_configured_base(self, monkeypatch):
        """Matched against the rule rather than a literal — `.env.test`
        configures different bases on different checkouts, so a test naming
        `storyline_test` would need editing per host.

        Reached by patching the module global rather than through a `base=`
        parameter no production caller passed: a seam that exists only for the
        one test exercising it proves the seam, not the rule.
        """
        monkeypatch.setattr("tests.conftest.TEST_DB_BASE_NAME", "other_base")
        assert is_own_convention("other_base_0123456789") is True
        assert is_own_convention(f"{TEST_DB_BASE_NAME}_0123456789") is False


class TestTheLockKey:
    def test_different_databases_get_different_keys(self):
        keys = {session_database_lock_key(_stray_name()) for _ in range(200)}
        assert len(keys) == 200

    def test_it_is_the_documented_derivation(self):
        """Pinned so the derivation cannot be swapped for another hash in a
        refactor: a changed key means a running session's lock becomes
        unfindable, and every live database reads as unowned — silently, and in
        the destructive direction."""
        name = "storyline_test_0123456789"
        expected = int.from_bytes(
            hashlib.blake2b(name.encode("utf-8"), digest_size=8).digest(),
            "big",
            signed=True,
        )
        assert session_database_lock_key(name) == expected
        # `pg_advisory_lock(bigint)` raises rather than wrapping out of range.
        assert -(2**63) <= expected < 2**63


class TestTheDatabaseScopingGuard:
    """Advisory locks are DATABASE-scoped, and that fails silently in the
    destructive direction.

    Measured on this cluster (PG 15.19), one key, three sessions: the holder on
    `postgres` gets True, a second connection on ANOTHER database also gets
    True — no contention whatsoever — and only a second connection on `postgres`
    gets False. `pg_locks` shows two granted rows identical in
    classid/objid/objsubid and differing only in `database`.

    A reaper running on the wrong database therefore finds every key free and
    drops live runs while looking like it works.
    """

    def test_a_maintenance_connection_is_accepted(self, cluster):
        assert_maintenance_scoped(cluster, "test")

    def test_a_connection_on_another_database_is_refused(
        self, cluster, a_stray_database
    ):
        other = connection_to(a_stray_database)
        try:
            with pytest.raises(RuntimeError, match="DATABASE-scoped"):
                assert_maintenance_scoped(other, "test")
        finally:
            other.close()

    def test_the_refusal_names_the_database_it_wanted(self, cluster, a_stray_database):
        """A bare RuntimeError is equally raised by a typo or a closed
        connection; naming both databases is what makes the failure actionable."""
        other = connection_to(a_stray_database)
        try:
            with pytest.raises(RuntimeError) as exc:
                assert_maintenance_scoped(other, "the_door")
            message = str(exc.value)
            assert "the_door" in message
            assert MAINTENANCE_DB in message and a_stray_database in message
        finally:
            other.close()


class TestOwnershipProtectsAnIdleDatabase:
    """THE PROPERTY THE WHOLE DESIGN RESTS ON.

    A live session's database is idle — zero live backends — for roughly a tenth
    of every run. So the question a reaper must answer is *does anyone own this*,
    and the question `count(backends)` answers is *is anyone querying this right
    now*. These tests hold the first true while forcing the second to zero,
    which is precisely the state that breaks the naive predicate.
    """

    def test_a_held_lock_protects_a_database_with_zero_backends(
        self, cluster, a_stray_database
    ):
        """Zero backends, and still not reapable, because someone owns it.

        Asserted as a PAIR with the released case below in
        `test_releasing_the_lock_is_what_makes_it_reapable` — either half alone
        is satisfied by a constant function, and the fix is a distinction.
        """
        owner = maintenance_connection()
        try:
            with owner.cursor() as cur:
                cur.execute(
                    "SELECT pg_try_advisory_lock(%s)",
                    (session_database_lock_key(a_stray_database),),
                )
                assert cur.fetchone()[0] is True, "the owner failed to take its lock"

            with cluster.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = %s",
                    (a_stray_database,),
                )
                backends = cur.fetchone()[0]
            assert backends == 0, "precondition: the stray must be idle"

            names = [n for n, _ in unowned_strays(cluster, settings.TEST_DB_NAME)]
            assert a_stray_database not in names
        finally:
            owner.close()

    def test_releasing_the_lock_is_what_makes_it_reapable(
        self, cluster, a_stray_database
    ):
        """The positive control for the test above, and the two together are the
        claim. Same database, same zero backends, opposite verdicts — and the
        only thing that changed is whether a session is holding it."""
        key = session_database_lock_key(a_stray_database)
        owner = maintenance_connection()
        try:
            with owner.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s)", (key,))
                assert cur.fetchone()[0] is True, "the owner never took the lock"
            held = [n for n, _ in unowned_strays(cluster, settings.TEST_DB_NAME)]
        finally:
            owner.close()

        released = [n for n, _ in unowned_strays(cluster, settings.TEST_DB_NAME)]

        assert (a_stray_database in held, a_stray_database in released) == (
            False,
            True,
        )

    def test_a_crashed_owner_stops_protecting_its_database(
        self, cluster, a_stray_database
    ):
        """The property that makes this reapable at all rather than a leak with
        extra steps: the lock dies with the backend, no cleanup path required.

        Closing the connection here stands in for the crash; measured directly
        under SIGKILL on this cluster for #785, with the same result.
        """
        owner = maintenance_connection()
        with owner.cursor() as cur:
            cur.execute(
                "SELECT pg_try_advisory_lock(%s)",
                (session_database_lock_key(a_stray_database),),
            )
            assert cur.fetchone()[0] is True, "the owner never took the lock"
        assert a_stray_database not in [
            n for n, _ in unowned_strays(cluster, settings.TEST_DB_NAME)
        ]

        owner.close()

        names = [n for n, _ in unowned_strays(cluster, settings.TEST_DB_NAME)]
        assert a_stray_database in names

    def test_a_scan_run_by_the_owner_still_excludes_the_owners_database(
        self, cluster, a_stray_database
    ):
        """ADVISORY LOCKS ARE RE-ENTRANT, and in production the scan runs on the
        very connection holding the lock — so the ownership probe answers True
        for our OWN database and the name exclusion is the only thing that
        removes it from the list.

        This is asserted from a connection that HOLDS the lock, because that is
        what production does (`setup_test_database` scans on `ownership_conn`).
        An earlier version scanned from `cluster`, a non-holding connection, so
        the lock excluded the database and the test passed with the name check
        deleted — green for a reason unrelated to its name. Measured: the same
        session taking its own key twice gets True both times.
        """
        owner = maintenance_connection()
        try:
            key = session_database_lock_key(a_stray_database)
            with owner.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s)", (key,))
                assert cur.fetchone()[0] is True
                cur.execute("SELECT pg_try_advisory_lock(%s)", (key,))
                assert cur.fetchone()[0] is True, "re-entrancy is the premise here"
                cur.execute("SELECT pg_advisory_unlock(%s)", (key,))
                cur.fetchone()

            names = [n for n, _ in unowned_strays(owner, mine=a_stray_database)]
            assert a_stray_database not in names
        finally:
            owner.close()

    def test_the_running_session_never_reaps_itself(self, cluster):
        """The real session, from outside it: this database is held, so a peer's
        scan cannot see it either."""
        names = [n for n, _ in unowned_strays(cluster, settings.TEST_DB_NAME)]
        assert settings.TEST_DB_NAME not in names


class TestTheReaperIsDisarmedByDefault:
    """Report-only unless armed, and that is a rollout guarantee rather than
    caution.

    The ownership signal only sees sessions running this code. Until every
    checkout on a host does, an unowned-looking database may be a live run from
    an older tree — the same "a concurrent pre-lock run may own it" window
    `_sweep_leftovers` documents, one suite over. Disarmed, the scan drops
    nothing and adds no catalog churn, which also means it cannot contribute to
    #773's shared-catalog race until someone arms it.
    """

    def test_it_does_not_drop_when_disarmed(
        self, cluster, a_stray_database, monkeypatch
    ):
        monkeypatch.delenv(REAP_ARMED_ENV, raising=False)
        assert reaping_is_armed() is False

        reported = reap_stray_databases(
            cluster, settings.TEST_DB_NAME, strays=[(a_stray_database, 0)]
        )

        assert a_stray_database in reported, "it must still SEE the stray"
        with cluster.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (a_stray_database,)
            )
            assert cur.fetchone() is not None, "disarmed, it must not drop"

    def test_it_drops_when_armed(self, cluster, a_stray_database, monkeypatch):
        """The other half — a switch that never acts is not a safe default, it
        is dead code, and only the pair distinguishes them.

        THE CANDIDATE LIST IS INJECTED, and that is not a style choice. An
        earlier version of this test called the scanning form, so arming it
        authorised dropping *every* unowned stray on the cluster — which it did,
        removing six databases on a shared development host while another
        checkout was running. Whose live database is indistinguishable from a
        corpse is the entire subject of this file; a test that arms the real
        door must therefore hand it exactly the one database it created.
        """
        monkeypatch.setenv(REAP_ARMED_ENV, "1")
        assert reaping_is_armed() is True

        reaped = reap_stray_databases(
            cluster, settings.TEST_DB_NAME, strays=[(a_stray_database, 0)]
        )

        assert a_stray_database in reaped
        with cluster.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (a_stray_database,)
            )
            assert cur.fetchone() is None

    def test_a_database_that_went_busy_survives_because_postgres_refuses(
        self, cluster, a_stray_database, monkeypatch
    ):
        """The scan and the drop are not simultaneous, and a run can start in
        between. **The protection here is PostgreSQL's, not this code's** —
        measured, `DROP DATABASE` against one live connection raises
        `ObjectInUse: database "..." is being accessed by other users`, and
        succeeds the instant that connection closes.

        This test is named for what actually holds. An earlier version of the
        reaper re-checked `pg_stat_activity` before dropping and called it a
        second filter that "strictly narrows"; deleting that check killed no
        test, because the server was already refusing. It was removed rather
        than kept with a test written to make it look load-bearing.

        Worth exactly what the measurement says: a live database is idle 8-12%
        of the time, so this is absent in precisely the case that matters. The
        ownership lock is the guarantee; this is a backstop that happens to be
        free.
        """
        monkeypatch.setenv(REAP_ARMED_ENV, "1")
        busy = connection_to(a_stray_database)
        try:
            reaped = reap_stray_databases(
                cluster, settings.TEST_DB_NAME, strays=[(a_stray_database, 0)]
            )
            assert a_stray_database not in reaped
            with cluster.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM pg_database WHERE datname = %s", (a_stray_database,)
                )
                assert cur.fetchone() is not None
        finally:
            busy.close()

    def test_armed_it_refuses_its_own_database_even_when_handed_it(
        self, cluster, a_stray_database, monkeypatch
    ):
        """The last line of defence: `mine` is excluded at the DROP, not only at
        the scan, so a caller that hands over its own database — or a future
        scan that stops excluding it — still cannot destroy the run in flight.

        **The subject is an IDLE database, and that is the whole point.** An
        earlier version passed `settings.TEST_DB_NAME`, this session's own
        database, which always has live backends at this moment — so PostgreSQL
        refused the drop and the test passed with the `mine` check deleted. It
        was green for a reason that had nothing to do with what it claimed to
        cover. Handing the reaper an idle database as `mine` removes the server's
        protection and leaves only the check under test.
        """
        monkeypatch.setenv(REAP_ARMED_ENV, "1")

        reaped = reap_stray_databases(
            cluster, mine=a_stray_database, strays=[(a_stray_database, 0)]
        )

        assert reaped == []
        with cluster.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (a_stray_database,)
            )
            assert cur.fetchone() is not None

    def test_arming_it_still_will_not_touch_a_foreign_database(
        self, cluster, monkeypatch
    ):
        """The ownership boundary outranks the switch. Armed is permission to
        drop OUR strays, never permission to drop whatever is lying around.

        Handed to the drop DIRECTLY, bypassing the scan, because the scan
        already filters on the same predicate — routing through it would test
        the filter twice and the destructive step not at all. This is the check
        that makes an injected list safe.
        """
        monkeypatch.setenv(REAP_ARMED_ENV, "1")
        foreign = f"{TEST_DB_BASE_NAME}_notours{uuid.uuid4().hex[:4]}"
        with cluster.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{foreign}"')
        try:
            assert is_own_convention(foreign) is False
            reaped = reap_stray_databases(
                cluster, settings.TEST_DB_NAME, strays=[(foreign, 0)]
            )

            assert foreign not in reaped
            with cluster.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (foreign,))
                assert cur.fetchone() is not None
        finally:
            with cluster.cursor() as cur:
                cur.execute(f'DROP DATABASE IF EXISTS "{foreign}"')

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", " 1 "])
    def test_the_usual_truthy_spellings_arm_it(self, monkeypatch, value):
        monkeypatch.setenv(REAP_ARMED_ENV, value)
        assert reaping_is_armed() is True

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
    def test_everything_else_leaves_it_disarmed(self, monkeypatch, value):
        """A destructive door reads an ambiguous value as OFF."""
        monkeypatch.setenv(REAP_ARMED_ENV, value)
        assert reaping_is_armed() is False


class TestTheScanNeverFailsTheRun:
    def test_the_composed_sweep_finds_a_real_stray_and_drops_nothing(
        self, cluster, a_stray_database, monkeypatch
    ):
        """THE SHIPPED PATH, end to end, with a real scan.

        Every other destructive test injects its candidate list to bound the
        blast radius, which means none of them exercises scan-then-report — the
        one composition the session fixture actually runs. Splitting the scan
        out of the reaper made the blast radius a property of which function you
        call; it did not by itself give the composed door a test, and a door
        reached only through a monkeypatched failure is a door nobody has opened.

        Safe to run unbounded precisely because it is DISARMED: the scan is a
        pure read and the report drops nothing, so the worst case is a printed
        list of somebody else's strays.
        """
        monkeypatch.delenv(REAP_ARMED_ENV, raising=False)

        reported = scan_and_reap_strays(cluster, settings.TEST_DB_NAME)

        assert a_stray_database in reported
        assert settings.TEST_DB_NAME not in reported
        with cluster.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (a_stray_database,)
            )
            assert cur.fetchone() is not None

    def test_a_broken_sweep_is_reported_and_swallowed(self, cluster, monkeypatch):
        """Hygiene that can redden a suite has inverted its own value — the
        whole point is to stop unexplained failures, not add a source of them.

        Asserted against `scan_and_reap_strays`, which is where the swallow
        lives. It is deliberately NOT inside `unowned_strays`: a library
        function that returns `[]` on error is indistinguishable from one that
        found nothing, and this file exists because two states that read alike
        are how the original defect survived.

        Deliberately narrow in the other direction too — `claim_session_database`
        is not swallowed, because a session that cannot prove it owns its own
        database is reapable by every peer, and continuing would be the bug.
        """
        monkeypatch.setattr(
            "tests.conftest.unowned_strays",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("catalog exploded")),
        )

        assert scan_and_reap_strays(cluster, settings.TEST_DB_NAME) == []


def test_the_convention_matches_what_this_session_actually_got():
    """Couples the predicate to the real artifact rather than to a restatement
    of it: the name in `settings` was generated by the conftest at import, not
    by this test, so a drift between generator and predicate shows up here."""
    assert is_own_convention(settings.TEST_DB_NAME) is True
