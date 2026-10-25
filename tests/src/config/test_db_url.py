"""`src.config.db_url`'s pooled-host predicate and refusal sentence, which the
worker and the migration runner share. No database: the predicate reads a host
name, so every case here is a string.

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
