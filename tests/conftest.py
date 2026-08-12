"""Pytest configuration and fixtures.

**The test database is named per session (#758).** Every checkout on a host
points at the same PostgreSQL, and this file owns a database BY NAME — creating
it, terminating every connection to it, and dropping it. With a fixed name, two
concurrent runs shared one database and whichever finished first ran that
teardown against the other's live session: connections killed mid-test, tables
dropped underneath a running suite, and nondeterministic red that each side
reads as their own code. The per-session suffix below removes the collision
rather than serialising around it.
"""

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

# Global flag to track if database is available
_database_available = None
_test_engine = None


def create_test_database():
    """Create test database if it doesn't exist."""
    # Connect to default postgres database to create test database
    conn = psycopg2.connect(
        host=settings.DB_HOST,
        port=settings.DB_PORT,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        database="postgres",
    )
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
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
    """Drop test database."""
    # Connect to default postgres database to drop test database
    conn = psycopg2.connect(
        host=settings.DB_HOST,
        port=settings.DB_PORT,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        database="postgres",
    )
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
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
    Session-scoped fixture to create test database before tests run.

    This fixture:
    1. Tries to create the test database if it doesn't exist
    2. Creates all tables using SQLAlchemy models
    3. Yields control to tests
    4. Drops the test database after all tests complete

    If database connection fails, yields None and allows pure unit tests to run.
    """
    global _database_available, _test_engine

    try:
        # Create test database
        create_test_database()

        # Create engine and tables
        engine = create_engine(settings.test_database_url)
        Base.metadata.create_all(engine)
        print("✓ Created all tables in test database")

        _database_available = True
        _test_engine = engine

        yield engine

        # Cleanup: drop all tables and database
        Base.metadata.drop_all(engine)
        engine.dispose()
        drop_test_database()

    except Exception as e:
        print(f"\n⚠️ Database setup skipped: {e}")
        print("   Pure unit tests will still run. Integration tests will be skipped.")
        _database_available = False
        _test_engine = None
        yield None


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
