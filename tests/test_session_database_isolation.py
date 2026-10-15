"""The test database is named per session (#758).

Every checkout on this host points at the same PostgreSQL, and `tests/conftest.py`
owns a database by name — it creates it, runs `pg_terminate_backend` against
every connection to it, and drops it. With a fixed name, two concurrent runs
would share one database, and whichever finished first would run that teardown
against the other's live session.
"""

import re

from src.config.settings import settings
from tests.conftest import SESSION_DB_SUFFIX, TEST_DB_BASE_NAME


def test_the_session_database_is_not_the_configured_shared_name():
    assert settings.TEST_DB_NAME != TEST_DB_BASE_NAME, (
        "the test database is using the configured shared name — two concurrent"
        " runs on this host will destroy each other's database"
    )


def test_the_name_is_the_configured_base_plus_a_session_suffix():
    """Matched against the RULE, not against a literal.

    `.env.test` overrides the `settings.py` default (`storydump_test`), and
    different checkouts configure different bases — so there are several
    collision groups rather than one. A test naming the value would need
    editing per host. This one cannot.
    """
    assert re.fullmatch(
        rf"{re.escape(TEST_DB_BASE_NAME)}_[0-9a-f]{{10}}", settings.TEST_DB_NAME
    ), settings.TEST_DB_NAME


def test_the_suffix_is_not_a_constant():
    """A suffix that were fixed would restore the collision while looking
    fixed — every session would still land on one name."""
    assert re.fullmatch(r"[0-9a-f]{10}", SESSION_DB_SUFFIX)
    assert SESSION_DB_SUFFIX != "0" * 10


def test_the_name_fits_a_postgres_identifier():
    """`CREATE DATABASE` silently truncates past 63 bytes, which would fold
    distinct sessions onto one name — the very collision the per-session
    suffix prevents."""
    assert len(settings.TEST_DB_NAME) <= 63


def test_the_url_every_consumer_reads_follows_the_name():
    """One home: nothing re-derives the database name, so the suffix has to
    reach consumers through `settings.test_database_url` itself."""
    assert settings.TEST_DB_NAME in settings.test_database_url
