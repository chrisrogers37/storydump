"""Pytest configuration and fixtures.

**The test database is named per session (#758).** Every checkout on a host
points at the same PostgreSQL, and this file owns a database BY NAME — creating
it, terminating every connection to it, and dropping it. With a fixed name, two
concurrent runs shared one database and whichever finished first ran that
teardown against the other's live session: connections killed mid-test, tables
dropped underneath a running suite, and nondeterministic red that each side
reads as their own code. The per-session suffix below removes the collision
rather than serialising around it.

**A database that dies is a failure, not a skip (#758 part 2).** The setup
fixture used to catch every exception and degrade to skipping the integration
tests. A run whose database vanished then reported ``N passed, M skipped`` at
``rc 0`` — so the tick, and "0 failed", both said the integration tests passed
when they had never executed. A false FAIL costs an hour and makes you look; a
false PASS makes you ship.

The discriminator is *did the server ever answer*, which is observable rather
than guessed: no PostgreSQL at the configured address means none was
configured, and skipping is honest. A server that answered and then failed is a
real failure and propagates.
"""

import hashlib
import os
import re
import socket
import uuid
from typing import NoReturn

import pytest
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

# Load test environment variables before importing any application code
load_dotenv(".env.test", override=True)

from src.config.database import Base  # noqa: E402
from src.config.settings import settings  # noqa: E402

#: One database name per session (#758), mirroring the ``runner_test_{uuid}``
#: pattern ``tests/scripts/conftest.py`` already proved for its scratch
#: databases — this was the one place still using a fixed name.
#:
#: Mutating the settings singleton is deliberate: it is the single point every
#: consumer already reads — this file's create and drop,
#: ``settings.test_database_url``, and the repository concurrency suite — so
#: there is no second name to keep in step. Assigned once, at import, before
#: anything can read the old value.
#:
#: The base is whatever the environment configured and is NOT assumed to be the
#: default in ``settings.py``: measured across the estate, ``.env.test`` sets it
#: to different values in different checkouts, so the name people collide on is
#: not one literal and checkouts fall into separate collision groups. Suffixing
#: whatever is configured makes that irrelevant — there is no name to look up
#: and no group to belong to.
TEST_DB_BASE_NAME = settings.TEST_DB_NAME
SESSION_DB_SUFFIX = uuid.uuid4().hex[:10]
settings.TEST_DB_NAME = f"{TEST_DB_BASE_NAME}_{SESSION_DB_SUFFIX}"

#: Set where integration coverage is MANDATORY — CI. It turns the one honest
#: local behaviour (no database, so skip) into a failure, because in CI "no
#: database" is not a contributor without PostgreSQL, it is the service
#: container failing to come up, and a run that quietly skips its integration
#: tests there is exactly the false PASS this file exists to stop.
REQUIRE_DB_ENV = "REQUIRE_TEST_DATABASE"

#: Upper bound on legitimately-skipped tests, enforced only where a database is
#: required. Verified against CI: five consecutive runs at exactly 10 skipped
#: while the passed count moved (2356 -> 2376); two of them (31648859395,
#: 31642935597) re-measured independently.
#:
#: **This number is hand-maintained and WILL drift** — that is its deliberate
#: cost, and it is why `integration_verdict` (which derives its input) is the
#: load-bearing gate and this is only a backstop. It catches mass-skip shapes
#: the verdict cannot see: a stray module-level skipmark, a missing optional
#: dependency. If you add a legitimate skip, raise this on purpose rather than
#: deleting the check.
#:
#: 10 -> 11 (#1195): `test_schema_drift_live.py` audits a LIVE database
#: against the target models and skips unless `SCHEMA_DRIFT_DSN` is
#: supplied, which only the scheduled `schema-drift` workflow does.
#: Skipping on pull requests is the designed behaviour, not a gap:
#: production legitimately lags `main` between merge and deploy, so a
#: PR-time assertion would be red as its normal state.
MAX_EXPECTED_SKIPS = 11

# Global flag to track if database is available
_database_available = None
_test_engine = None


def database_is_required() -> bool:
    return os.environ.get(REQUIRE_DB_ENV, "").strip().lower() in ("1", "true", "yes")


def integration_verdict(server_answered: bool, required: bool) -> str:
    """``run`` | ``skip`` | ``fail`` — the whole policy, in one place.

    Pure, so the policy is assertable with no PostgreSQL anywhere near it. The
    bug this replaces was a policy decision buried in an ``except Exception``:
    nothing could test it, and every failure mode collapsed into one answer.
    """
    if server_answered:
        return "run"
    return "fail" if required else "skip"


def skip_ceiling_breach(skipped: int, ceiling: int, required: bool):
    """The message for a mass skip, or None. Only meaningful where a database
    is required — elsewhere a large skip count is a contributor without one."""
    if not required or skipped <= ceiling:
        return None
    return (
        f"{skipped} tests were skipped, ceiling is {ceiling}. In an environment"
        f" that sets {REQUIRE_DB_ENV}, a skip count above the baseline is a"
        " swallowed database or a stray skipmark until proven otherwise — the"
        " run passed without executing the tests it claims to cover. Raise"
        " MAX_EXPECTED_SKIPS deliberately if the new skips are legitimate."
    )


def pytest_sessionfinish(session, exitstatus):
    """Fail the run on a mass skip. ``rc`` and "0 failed" are both blind to it."""
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:
        return
    breach = skip_ceiling_breach(
        len(reporter.stats.get("skipped", [])),
        MAX_EXPECTED_SKIPS,
        database_is_required(),
    )
    if breach:
        print(f"\n\u274c {breach}")
        session.exitstatus = 1


#: The database this suite's maintenance connections speak to. One spelling,
#: because the reaper's whole safety property is that owner and reaper share it
#: — advisory locks are DATABASE-scoped (see `assert_maintenance_scoped`), so a
#: second literal drifting from this one disarms the ownership check silently.
MAINTENANCE_DB = "postgres"


def connection_to(database: str):
    """An autocommit connection to ``database`` on the configured cluster.

    One home for the credential block, so a test that has to reach a database
    other than the maintenance one is not three hand-copied kwargs away from
    the real thing.
    """
    conn = psycopg2.connect(
        host=settings.DB_HOST,
        port=settings.DB_PORT,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        database=database,
    )
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    return conn


def maintenance_connection():
    """A connection to the maintenance database.

    One door, so the reachability probe and the create/drop pair cannot
    disagree about what "the server" means.
    """
    return connection_to(MAINTENANCE_DB)


#: How long the listener probe waits before concluding nothing is there. It runs
#: at most once per session, and only where libpq has already failed.
LISTENER_PROBE_TIMEOUT_S = 3


def server_is_listening(
    host: str, port: int, timeout: float = LISTENER_PROBE_TIMEOUT_S
) -> bool:
    """Is anything accepting TCP connections at this address?

    A property check, which is the whole point (#769). libpq reports *every*
    connection-phase failure as a connection error: measured on psycopg2 2.9.12
    / PostgreSQL 15.18, ``pgcode`` is ``None`` for a dead port, a bad password,
    an absent role, and a refused connection limit alike. Only the message text
    differs, and matching on that is a locale-dependent string match — the shape
    #758 rejected when it replaced the argv detector with a cwd check.

    Two calls decided deliberately rather than inherited (#769 asked for both):

    - A listener that is **not** PostgreSQL counts as answered. The question is
      "was a database configured *here*", and an occupied address is a better
      proxy for that than a completed login. If something else holds the port,
      ``DB_HOST``/``DB_PORT`` is wrong and a loud failure is the right outcome.
      Verifying the wire protocol instead was considered and rejected: it means
      implementing a fragment of the startup handshake inside a conftest.
    - A server that has *died* still reads as absent, because nothing listens.
      That ambiguity predates this change and is unchanged by it.
    """
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except OSError:
        return False


def server_answered() -> bool:
    """Did a PostgreSQL answer at the configured address?

    THE DISCRIMINATOR. It separates "no database was configured" — the only
    condition under which skipping the integration tests is honest — from "a
    database was configured and then failed", which is a real failure. Only
    ``OperationalError`` is caught: that is the could-not-connect class, and
    anything else here is a defect that must surface rather than be read as an
    absent server.

    **An ``OperationalError`` is not itself evidence of an absent server
    (#769).** ``too many connections``, ``password authentication failed`` and
    ``database does not exist`` all mean the server answered and refused.
    Reading those as not-configured skipped every integration test at ``rc 0``
    — the #758 false PASS, reproduced inside the probe meant to prevent it. So
    where libpq cannot say, the address is asked directly.

    That deliberately widens the fail branch: a half-configured environment
    (PostgreSQL up, test credentials wrong) now fails where it used to skip.
    That is the honest answer, since such a run executed no integration tests
    either way — so the cost is paid in the diagnosis printed below rather than
    in silence.

    Bound, stated rather than glossed: a server that has just died is still
    indistinguishable from one that was never there, because neither leaves
    anything listening. That single case reads as not-configured. Every later
    failure is unambiguous and is raised.
    """
    try:
        maintenance_connection().close()
        return True
    except psycopg2.OperationalError as exc:
        if server_is_listening(settings.DB_HOST, settings.DB_PORT):
            print(
                f"\n❌  A server IS listening at"
                f" {settings.DB_HOST}:{settings.DB_PORT} and refused the"
                f" connection — {exc}"
                "\n   This is NOT an absent database, so the integration tests"
                " will run and fail rather than skip (#769)."
                "\n   Check DB_USER / DB_PASSWORD / DB_NAME in .env.test, and"
                " that the role and database exist."
            )
            return True
        print(
            f"\n\u26a0\ufe0f  No PostgreSQL answered at"
            f" {settings.DB_HOST}:{settings.DB_PORT} — {exc}"
        )
        return False


def create_test_database():
    """Create this session's test database."""
    conn = maintenance_connection()
    cursor = conn.cursor()

    # Check if test database exists
    cursor.execute(
        "SELECT 1 FROM pg_database WHERE datname = %s", (settings.TEST_DB_NAME,)
    )
    exists = cursor.fetchone()

    if not exists:
        cursor.execute(f"CREATE DATABASE {settings.TEST_DB_NAME}")
        print(f"\n✓ Created test database: {settings.TEST_DB_NAME}")
    else:
        print(f"\n✓ Test database already exists: {settings.TEST_DB_NAME}")

    cursor.close()
    conn.close()


def drop_test_database():
    """Drop this session's test database."""
    conn = maintenance_connection()
    cursor = conn.cursor()

    # Terminate all connections to test database
    cursor.execute(f"""
        SELECT pg_terminate_backend(pg_stat_activity.pid)
        FROM pg_stat_activity
        WHERE pg_stat_activity.datname = '{settings.TEST_DB_NAME}'
        AND pid <> pg_backend_pid()
    """)

    # Drop database
    cursor.execute(f"DROP DATABASE IF EXISTS {settings.TEST_DB_NAME}")
    print(f"\n✓ Dropped test database: {settings.TEST_DB_NAME}")

    cursor.close()
    conn.close()


#: Arms the DESTRUCTIVE half of the stray reaper. Report-only by default, and
#: that default is a rollout guarantee rather than timidity — see
#: `reap_stray_databases`. Mirrors `dedup-media`'s dry-run-by-default shape.
REAP_ARMED_ENV = "REAP_ORPHAN_TEST_DATABASES"


def precondition_absent(reason: str) -> NoReturn:
    """Skip — except where this environment declared integration coverage
    mandatory, and a missing precondition is then a FAILURE.

    Lives here rather than in a test module because it now has two consumers
    (#804's refusal reproduction and #758's reaper tests) and it encodes a
    policy, not a convenience. `tests/scripts/` skips unconditionally on the
    same class of missing privilege; this diverges because a guard that quietly
    does not run cannot be told apart from one that ran and found nothing —
    which is the defect both issues were filed about.

    DELIBERATELY NOT ROUTED THROUGH `integration_verdict`, 40 lines up, which
    encodes the same two negative rows. Calling it would mean writing
    `server_answered=False` for a precondition like a missing CREATEROLE — where
    a server demonstrably DID answer and merely lacked a privilege. A false
    argument to buy a shared spelling is the worse trade; the shared part that
    can actually drift, `database_is_required()`, is called rather than
    re-parsed.
    """
    if database_is_required():
        pytest.fail(
            f"{reason}\nIntegration coverage is mandatory here ({REQUIRE_DB_ENV}"
            " is set), so a missing precondition fails rather than skips: a"
            " guard that silently skips in CI is the absence it was written to"
            " prevent, with a test file around it."
        )
    pytest.skip(reason)


def require_role_privilege(conn, column: str, flag: str) -> None:
    """Skip — or fail where a database is required — unless the current role is
    SUPERUSER or holds ``column``.

    One home for a probe that was on its way to a third copy: #804's refusal
    reproduction needs CREATEROLE, #758's reaper tests need CREATEDB, and
    `tests/scripts/conftest.py::actor_lacks_createrole` is the same query again.
    That third copy stays put — it is outside this change and importable in
    neither direction — but the two root-suite copies collapse here.

    CI's `postgres:15` service makes `POSTGRES_USER` the cluster superuser, so
    these gates genuinely run there; a local role created by hand often has
    neither flag, and a silent pass would read as coverage that was never taken.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT rolsuper, {column} FROM pg_roles WHERE rolname = current_user"
        )
        row = cur.fetchone()
    if row and (row[0] or row[1]):
        return
    precondition_absent(
        f"{settings.DB_USER} lacks {flag}: needs SUPERUSER or {flag}."
        f" Grant it with: ALTER ROLE {settings.DB_USER} {flag};"
    )


def reaping_is_armed() -> bool:
    return os.environ.get(REAP_ARMED_ENV, "").strip().lower() in ("1", "true", "yes")


def is_own_convention(datname: str) -> bool:
    """Is this a database THIS suite's naming convention could have created?

    The ownership boundary, and the only thing standing between a sweep and
    somebody's real data. Anchored on the exact shape #763 generates —
    ``<configured base>_<10 lowercase hex>`` — rather than a prefix test,
    because `LIKE 'storyline_test%'` also matches a hand-made
    `storyline_test_snapshot` or `storyline_testing`, and a reaper is not
    entitled to guess.

    Sibling of `tests/scripts/conftest.py::_is_suite_db`, deliberately not
    shared with it: that predicate names a different convention
    (``runner_test_``/``runner_tpl_``) and the two must never widen into each
    other. Measured on the live cluster: all six root-suite strays present
    today return False from `_is_suite_db`, which is correct — they are not its
    to reap, and it is not ours to teach about them.
    """
    return (
        re.fullmatch(rf"{re.escape(TEST_DB_BASE_NAME)}_[0-9a-f]{{10}}", datname)
        is not None
    )


def session_database_lock_key(datname: str) -> int:
    """The advisory-lock key a session holds for the whole life of its database.

    Derived from the name so the owner and any reaper compute it identically
    with nothing to look up and nothing to keep in step. blake2b rather than
    `hash()` (per-process salt) or `hashtext()` (an internal PostgreSQL
    function with no cross-version stability contract).
    """
    digest = hashlib.blake2b(datname.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def assert_maintenance_scoped(conn, door: str) -> None:
    """Refuse a lock operation on a connection that is not in the maintenance
    database.

    **PostgreSQL advisory locks are DATABASE-scoped, not cluster-scoped**, and
    getting this wrong fails silently in the worst possible direction: the
    reaper's probe would succeed against a database whose owner is holding the
    very same key from elsewhere, and it would drop a live run while looking
    like it worked. Measured on this cluster (PG 15.19), one key, three
    sessions:

    | prober | `pg_try_advisory_lock` |
    |---|---|
    | holder, on `postgres` | True |
    | second connection, on ANOTHER database | **True** — no contention at all |
    | second connection, on `postgres` | False — contends |

    `pg_locks` confirms it: two granted rows, identical `classid`/`objid`/
    `objsubid`, differing only in `database`.

    This is also why `SUITE_CLUSTER_LOCK_KEY` works one directory over — every
    holder reaches it through `maintenance_conn`, which always connects to
    `postgres`. That is currently a property of how its callers are written; here
    it is checked, for the same reason #808 checked rather than declared.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT current_database()")
        current = cur.fetchone()[0]
    if current != MAINTENANCE_DB:
        raise RuntimeError(
            f"{door}: advisory locks are DATABASE-scoped, so this must run on"
            f" {MAINTENANCE_DB!r} to contend with database owners — got"
            f" {current!r}. On the wrong database every probe succeeds and the"
            " reaper drops live runs silently."
        )


def lock_holder(cur, key: int):
    """Who holds advisory lock ``key``: ``(pid, usename, application_name)``.

    A deliberate second copy of `tests/scripts/conftest.py::lock_holder` rather
    than an import: that module already imports FROM this one, so the reverse is
    a circular import, and it does filesystem work at import time (it walks the
    migrations corpus) that every `pytest -m unit` run would then pay for.

    Matched on the FULL identity, not on ``objid`` alone — measured there, a
    two-argument `pg_advisory_lock(0, k)` lands on the same ``objid`` as our
    one-argument bigint and is distinguished only by ``objsubid``.
    """
    cur.execute(
        "SELECT a.pid, a.usename, a.application_name"
        " FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid"
        " WHERE l.locktype = 'advisory' AND l.granted"
        "   AND l.classid = %s AND l.objid = %s AND l.objsubid = 1",
        ((key >> 32) & 0xFFFFFFFF, key & 0xFFFFFFFF),
    )
    return cur.fetchone()


def claim_session_database(conn) -> None:
    """Hold this session's ownership lock for as long as the session lives.

    Session-level (not transaction-level) so it survives every idle stretch,
    which is the entire point — see `reap_stray_databases` for what idleness
    does to the obvious alternative. Released by the connection closing, which
    also covers a hard kill: measured on this cluster for #785, SIGKILL the
    holder and the lock is gone with no cleanup path run.
    """
    assert_maintenance_scoped(conn, "claim_session_database")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_try_advisory_lock(%s)",
            (session_database_lock_key(settings.TEST_DB_NAME),),
        )
        if not cur.fetchone()[0]:
            holder = lock_holder(cur, session_database_lock_key(settings.TEST_DB_NAME))
            raise RuntimeError(
                f"could not claim the ownership lock for {settings.TEST_DB_NAME}"
                f" — held by {holder}. Refusing to run unowned rather than"
                " proceeding reapable by every peer.\n"
                "A LIVE PEER HOLDING A COLLIDING KEY is the only cause this can"
                " have: the name carries a fresh per-session uuid, a dead"
                " backend releases its lock with no cleanup path (measured under"
                " SIGKILL for #785), and a hold by THIS session would be"
                " re-entrant and succeed. Naming the holder is the whole reason"
                " an advisory lock beats a file lock here."
            )


def unowned_strays(conn, mine: str) -> list:
    """Databases of this suite's convention that no live session is holding.

    Returns ``[(datname, backends)]``.

    **WHAT THIS PREDICATE ACTUALLY ANSWERS**, which is not the same as what it
    is being used to mean. It answers: *no session connected to this cluster's
    maintenance database is currently holding the key derived from this name.*
    It is being used to mean *nobody owns this database.* The gap is sessions
    that never took the lock — a checkout running code older than this change.
    That window is real, it is why the destructive half ships disarmed, and it
    closes on its own once every checkout holds the lock rather than by anyone
    remembering.

    The backend count is reported and deliberately NOT the gate. Measured over
    two full live sessions, sampling `pg_stat_activity` every 200 ms against the
    session's own database:

    | run | samples | zero-backend | longest zero window |
    |---|---|---|---|
    | 1 | 288 (~58 s) | 35 (**12.2%**) | contiguous |
    | 2 | 253 (~51 s) | 22 (**8.7%**) | ~**2.6 s** |

    A test database is idle for long stretches — the unit tests never touch it
    and SQLAlchemy returns pooled connections — so `count(backends) == 0`
    answers *is anyone querying this right now*, not *does anyone own this*.
    Those diverge for roughly a tenth of every run.

    `tests/scripts/_sweep_leftovers` gates on exactly that count and is safe
    anyway, because no concurrent session of that suite can exist while it holds
    `SUITE_CLUSTER_LOCK_KEY`. **Its predicate is incidentally correct; its mutex
    is what guarantees it** — its own message says as much ("a concurrent
    pre-lock run may own it"). The root suite holds no such mutex, so copying
    the predicate here would move the check without the guarantee, and a
    backend-count reaper would drop a peer's live database at roughly 1-in-10
    per sweep. The victim sees connection-loss errors in files they never
    touched: the original #758 symptom, recreated by the fix for it and
    attributed to someone else.
    """
    assert_maintenance_scoped(conn, "unowned_strays")
    with conn.cursor() as cur:
        # No SQL LIKE pre-filter. It was a SECOND, looser encoding of the
        # convention that `is_own_convention` already owns, and measured on this
        # cluster the obvious spelling is wrong in the permissive direction:
        # `'storylineXtest_0123456789' LIKE 'storyline_test\_%'` is TRUE,
        # because only the appended separator was escaped and the base's own
        # underscores stayed single-character wildcards. Harmless while the
        # regex re-filters, and exactly the prefix rule this module rejects.
        # `pg_database` has tens of rows; filtering in Python costs nothing.
        cur.execute(
            "SELECT d.datname, count(a.pid) FROM pg_database d"
            " LEFT JOIN pg_stat_activity a ON a.datname = d.datname"
            " GROUP BY d.datname ORDER BY d.datname"
        )
        # `r[0] != mine` is NOT redundant with the lock probe below. In
        # production this scan runs on the connection that HOLDS this session's
        # lock, and advisory locks are re-entrant — measured, the same session
        # takes its own key twice and gets True both times — so our own database
        # probes free and would be listed as a stray. The drop refuses it a
        # second time; this is what keeps the REPORT honest.
        rows = [r for r in cur.fetchall() if is_own_convention(r[0]) and r[0] != mine]

        unowned = []
        for datname, backends in rows:
            key = session_database_lock_key(datname)
            cur.execute("SELECT pg_try_advisory_lock(%s)", (key,))
            if not cur.fetchone()[0]:
                continue
            cur.execute("SELECT pg_advisory_unlock(%s)", (key,))
            unowned.append((datname, backends))
    return unowned


def reap_stray_databases(conn, mine: str, strays) -> list:
    """Report — and, only when armed, drop — crashed runs' leftover databases.

    Returns the names acted on or reported.

    **Report-only by default, and that is a rollout guarantee rather than
    caution.** The ownership signal only sees sessions running this code; until
    every checkout on a host does, an unowned-looking database may be a live run
    from an older tree. Disarmed, this is a pure read: it opens no transaction,
    drops nothing, and adds no catalog churn — which also means it cannot
    contribute to the #773 shared-catalog race until someone arms it. **Arm it
    only once every checkout on the host is on this code**; there is no way for
    this function to check that, which is exactly why it is not the default.

    ``strays`` is injectable, and that parameter exists because of a real
    incident rather than for tidiness. Called with the default it SCANS, so its
    blast radius is every unowned stray on the cluster — correct for the session
    fixture and catastrophic for a test, which is what a test arming this
    discovered by dropping six databases on a shared host mid-development.
    Anything that wants to arm this against one database passes that one
    database.

    EVERY precondition is re-checked HERE rather than trusted from the scan —
    name, ownership boundary AND the ownership lock — because the drop is the
    destructive step and must verify its own preconditions instead of
    inheriting them from a caller.

    The lock re-check was missing when this first shipped, and the docstring
    claimed the boundary could not be widened by an injected list. It could:
    review handed this an injected list holding a peer-locked, idle,
    convention-matching database and it was dropped, because the guards
    checked only the name. Stated precisely now, because the previous wording
    was the thing a reader would have trusted instead of the guard.

    **The lock is the only ownership guarantee here, and nothing else pretends
    to be one.** A database being actively queried is protected, but by
    PostgreSQL refusing the DROP rather than by anything in this function — and
    that protection is worth exactly what the measurement says it is worth: a
    live session's database is idle 8-12% of the time, so it is absent precisely
    when it would matter.

    Never fails the run. A reaper is hygiene, and a hygiene step that can redden
    a suite has inverted its own value.
    """
    if not strays:
        return []

    armed = reaping_is_armed()
    verb = "Dropping" if armed else "Would drop"
    print(
        f"\n🧹 {len(strays)} orphaned test database(s) from crashed runs"
        f" — {verb} (arm with {REAP_ARMED_ENV}=1):"
    )
    assert_maintenance_scoped(conn, "reap_stray_databases")
    acted = []
    for datname, backends in strays:
        if datname == mine or not is_own_convention(datname):
            print(f"   REFUSING {datname} — not this suite's to drop")
            continue
        print(f"   {datname}  (backends={backends}, no live owner)")
        if not armed:
            acted.append(datname)
            continue
        try:
            # No re-check that the database is still IDLE: PostgreSQL enforces
            # that itself and atomically, which a check here cannot. Measured —
            # DROP against a database with one live connection raises
            # `ObjectInUse`, and succeeds the moment that connection closes. An
            # earlier version did re-check and killed no mutant.
            #
            # The OWNERSHIP lock IS re-checked, and that is a different matter.
            # Found in review: the guards above test the name and nothing else,
            # so an injected list containing a peer-locked, idle,
            # convention-matching database was dropped — while this function's
            # docstring claimed an injected list could never widen the boundary.
            # A safety claim wider than what the code checks is the same defect
            # as a harness authorising more than intended, and worse in one way:
            # it is what the next reader relies on instead of reading the guard.
            with conn.cursor() as cur:
                key = session_database_lock_key(datname)
                cur.execute("SELECT pg_try_advisory_lock(%s)", (key,))
                if not cur.fetchone()[0]:
                    print(
                        f"   REFUSING {datname} — a live session holds its"
                        " ownership lock"
                    )
                    continue
                try:
                    cur.execute(f'DROP DATABASE IF EXISTS "{datname}"')
                finally:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (key,))
            acted.append(datname)
        except Exception as exc:  # noqa: BLE001 - hygiene must never fail
            print(f"   ...could not drop {datname}: {exc}")
    return acted


def scan_and_reap_strays(conn, mine: str) -> list:
    """The composition the session fixture runs: scan, then report-or-drop.

    Separate from `reap_stray_databases` because `strays` used to default to
    None and mean "scan the whole cluster" — a mode switch hiding in a data
    parameter, whose dangerous mode was the default. A test that armed it
    therefore authorised dropping every unowned database on the cluster, which
    it did: six of them, on a shared host, while another checkout was running.
    Splitting makes the blast radius a property of which function you call.

    The swallow lives HERE rather than inside the scan, so a library function
    returning ``[]`` never means two different things to a future caller. A
    reaper is hygiene, and hygiene that can redden a suite has inverted its own
    value.
    """
    try:
        return reap_stray_databases(conn, mine, unowned_strays(conn, mine))
    except Exception as exc:  # noqa: BLE001 - hygiene must never fail the run
        print(f"\n⚠️  stray-database sweep skipped on error: {exc}")
        return []


@pytest.fixture(scope="session", autouse=True)
def setup_test_database():
    """
    Session-scoped fixture owning this session's test database.

    1. Probes whether a PostgreSQL answers at all (`server_answered`) — the
       skip-or-fail discriminator.
    2. Creates this session's uniquely-named database and builds the schema
       from the SQLAlchemy models.
    3. Yields the engine to tests.
    4. Drops the schema and the database after all tests complete.

    THREE outcomes rather than two, and the third is the point:

    - **server answered** — run.
    - **no server, database not required** — yield None; consumers skip. The
      only condition under which skipping integration tests is honest.
    - **no server, database required** (`REQUIRE_TEST_DATABASE`) — raise. In CI
      "no database" is not a contributor without PostgreSQL, it is the service
      container failing to come up, and skipping there produces a green run
      that never executed its integration tests.

    Nothing after the probe is swallowed: a database that answered and then
    failed is a real failure and propagates.
    """
    global _database_available, _test_engine

    verdict = integration_verdict(server_answered(), database_is_required())

    if verdict == "fail":
        raise RuntimeError(
            f"{REQUIRE_DB_ENV} is set, so integration coverage is mandatory"
            f" here, but no PostgreSQL answered at {settings.DB_HOST}:"
            f"{settings.DB_PORT}. Failing rather than skipping: a run that"
            " silently skips its integration tests still exits 0 and reads as"
            " a pass (#758)."
        )

    if verdict == "skip":
        print("   Pure unit tests will still run. Integration tests will be skipped.")
        _database_available = False
        _test_engine = None
        yield None
        return

    # The server answered. NOTHING below is swallowed — every failure from here
    # is a database that WAS configured and then went wrong, which is exactly
    # the case the old `except Exception` turned into a green run.
    #
    # Claim BEFORE reaping, and reap before creating: the claim is what makes
    # this session invisible to every other session's reaper, and taking it
    # first means there is no instant at which our own database exists unowned.
    # A connection rather than a flag, because the lock has to be held by a live
    # backend to mean anything. A generator's locals live across the `yield`, so
    # this stays open for the whole session without a module global.
    ownership_conn = maintenance_connection()
    claim_session_database(ownership_conn)
    scan_and_reap_strays(ownership_conn, settings.TEST_DB_NAME)

    create_test_database()
    engine = create_engine(settings.test_database_url)
    Base.metadata.create_all(engine)
    print("✓ Created all tables in test database")

    _database_available = True
    _test_engine = engine

    yield engine

    # Teardown sits OUTSIDE any except-and-yield path deliberately: the old
    # shape yielded a SECOND time when teardown raised, which pytest reports as
    # an unreadable fixture error instead of the cleanup failure it is.
    Base.metadata.drop_all(engine)
    engine.dispose()
    drop_test_database()

    # Release the ownership lock LAST — while it is held, no other session's
    # reaper can consider this database unowned, and the drop above is the point
    # after which there is nothing left to protect.
    ownership_conn.close()


@pytest.fixture(scope="function")
def test_db(setup_test_database):
    """
    Function-scoped fixture providing a clean database session for each test.

    Each test gets a fresh transaction that is rolled back after the test completes,
    ensuring test isolation without recreating tables.

    If database is not available, skips the test.
    """
    if setup_test_database is None:
        pytest.skip("Database not available - skipping integration test")

    TestSessionLocal = sessionmaker(bind=setup_test_database)
    session = TestSessionLocal()

    # Begin a nested transaction
    session.begin_nested()

    yield session

    # Rollback everything from the test
    session.rollback()
    session.close()


@pytest.fixture
def sample_media_item():
    """Sample media item for testing."""
    return {
        "file_path": "/test/media/image.jpg",
        "file_name": "image.jpg",
        "file_hash": "abc123def456",
        "file_size_bytes": 1024000,
        "mime_type": "image/jpeg",
    }


@pytest.fixture()
def route_repos_to_test_db(setup_test_database, monkeypatch):
    """Route the production session factory at the test DB (shared home —
    the per-file copies of this fixture predate it and can migrate here)."""
    if setup_test_database is None:
        pytest.skip("Database not available - skipping integration test")

    from sqlalchemy.orm import sessionmaker

    import src.config.database as db_module

    monkeypatch.setattr(
        db_module,
        "SessionLocal",
        sessionmaker(
            autocommit=False,
            autoflush=False,
            bind=setup_test_database,
            expire_on_commit=False,
        ),
    )
    yield


def make_tenant():
    """Create a real chat_settings row — chat_settings_id is a live FK.

    Returns (chat_settings_id, telegram_chat_id); caller cleans up via
    delete_tenants().
    """
    import random

    from src.repositories.chat_settings_repository import ChatSettingsRepository

    telegram_chat_id = -random.randint(10**11, 10**12)
    repo = ChatSettingsRepository()
    try:
        settings = repo.get_or_create(telegram_chat_id)
        return str(settings.id), telegram_chat_id
    finally:
        repo.close()


def delete_tenants(tenant_ids):
    from sqlalchemy import text

    from src.repositories.chat_settings_repository import ChatSettingsRepository

    repo = ChatSettingsRepository()
    try:
        for tid in tenant_ids:
            repo.db.execute(
                text("DELETE FROM chat_settings WHERE id = :tid"), {"tid": tid}
            )
        repo.db.commit()
    finally:
        repo.close()
