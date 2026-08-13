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

import os
import socket
import uuid

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
MAX_EXPECTED_SKIPS = 10

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


def maintenance_connection():
    """A connection to the ``postgres`` maintenance database.

    One door, so the reachability probe and the create/drop pair cannot
    disagree about what "the server" means.
    """
    conn = psycopg2.connect(
        host=settings.DB_HOST,
        port=settings.DB_PORT,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        database="postgres",
    )
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    return conn


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
