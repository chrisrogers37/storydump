"""`src.config.db_url`'s pooled-host predicate and refusal sentence, which the
worker and the migration runner share, and its rule for an ambiguous URL and
refusal of a URL a driver cannot read, which every process that connects
shares. No database: the predicates read a host name or a URL, so every case
here is a string.

The names are reached through the module, not imported, so that on a tree
without them each test fails on its own instead of the session failing to
collect."""

from __future__ import annotations

import pytest

from src.config import db_url

#: Placeholders in Neon's shape, never a real endpoint: the repository is public.
POOLED = "ep-xxx-pooler.region.aws.neon.tech"
DIRECT = "ep-xxx.region.aws.neon.tech"


@pytest.mark.parametrize(
    "host",
    [
        POOLED,
        POOLED.upper(),
        f"{POOLED}:5432",  # SQLAlchemy's multi-host form carries the port
        f" {POOLED} ",
        # libpq's (and PGHOST's) comma-separated list: one pooled host is enough
        f"{DIRECT},{POOLED}",
        "my-pooler",  # a bare name is its own first label
    ],
)
def test_a_pooled_host_is_recognised(host):
    assert db_url.is_pooled_host(host) is True


@pytest.mark.parametrize(
    "host",
    [
        DIRECT,
        f"{DIRECT}:5432",
        "localhost",
        "127.0.0.1",
        "::1",
        "[::1]:5432",
        "/var/run/postgresql",  # a socket directory names no host
        # the mark is the FIRST label's suffix, not a word anywhere in the name
        "db.my-pooler.example.com",
        "pooler-ep.example.com",
        "ep-xxx-poolerx.region.aws.neon.tech",
        "",
        None,
    ],
)
def test_a_direct_or_absent_host_is_not_pooled(host):
    assert db_url.is_pooled_host(host) is False


def test_the_refusal_names_the_variable_the_reason_and_the_fix():
    message = db_url.pooled_endpoint_refusal("TARGET_DATABASE_URL")
    assert message.startswith("TARGET_DATABASE_URL names a pooled database endpoint")
    assert db_url.POOLED_HOST_LABEL_SUFFIX in message
    assert "session-scoped advisory lock" in message
    assert "direct host" in message


@pytest.mark.parametrize(
    "url",
    [
        # an unencoded `@` in the password, wherever the drivers end it
        "postgresql://owner:not-a-real-password@password@db.invalid/db",
        "postgresql://owner:not-a-real-password@@password@db.invalid/db",
        "postgresql://owner:not-a-real-password@not-a-real,@db.invalid/db",
        "postgresql://owner:not-a-real-password@[not-a-real]password@db.invalid/db",
        "postgresql://owner:not-a-real-password@not-a-real?password@db.invalid/db",
        # an unencoded `@` in the user
        "postgresql://own@er:not-a-real-password@db.invalid/db",
        # the accepted cost: a raw `@` in a query value beside a user's
        "postgresql://owner@/db?host=/run/user@1000",
        "postgresql://owner:not-a-real-password@/db?host=@pgsock",
        # any scheme, SQLAlchemy's among them
        "postgresql+asyncpg://owner:not-a-real-password@password@db.invalid/db",
        "postgres://owner:not-a-real-password@password@db.invalid/db",
    ],
)
def test_a_url_with_a_raw_at_sign_besides_the_one_before_the_host_is_ambiguous(url):
    assert db_url.is_ambiguous_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://owner:not-a-real-password@db.invalid/db",
        "postgresql://owner@db.invalid/db",
        "postgresql://db.invalid/db",
        # every other `@` encoded, in the password and in a query value
        "postgresql://owner:not-a-real%40password@db.invalid/db",
        "postgresql://owner@/db?host=/run/user%401000",
        "postgresql://owner@/db?host=%40pgsock",
        # with no user, the one `@` may stand in a query value
        "postgresql:///db?host=/run/user@1000",
        # a raw `?`, `#`, `[` or `/` in the password is no `@`
        "postgresql://owner:not-a-real?password@db.invalid/db",
        "postgresql://owner:not-a-real#password@db.invalid/db",
        "postgresql://owner:not-a-real[password@db.invalid/db",
        "postgresql://owner:not-a-real/password@db.invalid/db",
        # a key/value string carries no `://`, whatever its values carry
        "host=@pgsock dbname=db user=owner",
        "host=db.invalid user=own@er password=not-a-real@password",
        "",
    ],
)
def test_a_url_with_one_at_sign_or_a_key_value_string_is_not(url):
    assert db_url.is_ambiguous_url(url) is False


def test_the_unreadable_refusal_names_the_variable_the_cause_and_the_fixes():
    """One sentence for every cause, since the refusal never looks at the
    value to say which: the user information, the port, a query parameter."""
    message = db_url.unreadable_url_refusal("TARGET_DATABASE_URL")
    assert message.startswith(
        "TARGET_DATABASE_URL cannot be read as a database URL: write it as"
        " postgresql://user:password@host:port/database"
    )
    assert (
        "percent-encoding every @ but the one before the host and any / ? # or %"
        " in the user or password" in message
    )
    assert "check its port and each query parameter" in message


def test_the_unreadable_refusal_is_a_value_error_that_carries_only_the_sentence():
    refusal = db_url.UnreadableUrl("SOME_DSN")
    assert isinstance(refusal, ValueError)
    assert str(refusal) == db_url.unreadable_url_refusal("SOME_DSN")
    assert (
        repr(refusal) == f"UnreadableUrl({db_url.unreadable_url_refusal('SOME_DSN')!r})"
    )
