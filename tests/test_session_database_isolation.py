"""The test database is named per session (#758).

Every checkout on this host points at the same PostgreSQL, and `tests/conftest.py`
owns a database by name — it creates it, runs `pg_terminate_backend` against
every connection to it, and drops it. While that name was a fixed literal, two
concurrent runs shared one database and whichever finished first executed that
teardown against the other's live session.

Measured while this was still true: four consecutive runs of one unchanged
branch produced 2, 12, 4 and 9 errors, in different sets each time, in files the
branch never touched — and the same branch was green in CI.
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

    An earlier version of this test hardcoded `storydump_test` from
    `settings.py` and went red on its first run: `.env.test` overrides that
    default, and measured across the estate different checkouts configure
    different bases — so there are several collision groups rather than one.
    A test naming the value would need editing per host. This one cannot.
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
    distinct sessions back onto one name — the collision, reintroduced by the
    fix for it."""
    assert len(settings.TEST_DB_NAME) <= 63


def test_the_url_every_consumer_reads_follows_the_name():
    """One home: nothing re-derives the database name, so the suffix has to
    reach consumers through `settings.test_database_url` itself."""
    assert settings.TEST_DB_NAME in settings.test_database_url
