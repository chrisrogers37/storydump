"""The consistency job refuses an administrator URL libpq cannot read by its
name, never by its value.

libpq quotes the part of a connection string it could not read, which can be
the password. No database: the job's git steps are stood in for, and the URL
is refused before anything connects.
"""

import pytest

from scripts import schema_consistency
from tests.unreadable_urls import (
    LIBPQ_UNREADABLE,
    OWNER_URL,
    READABLE,
    REFUSAL,
    STAND_IN,
    assert_refused_by_name,
    assert_withheld,
    cases,
    stand_in_administrator,
)

#: How the job names its replay cluster's URL in a refusal.
REPLAY_URL = "REPLAY_DATABASE_URL (or --replay-database-url)"


@pytest.fixture
def no_git(monkeypatch):
    """The base revision and its archive, stood in for: the refusal comes
    when the job first connects, after both."""
    monkeypatch.setattr(schema_consistency, "base_revision", lambda root, pin: "0" * 40)
    monkeypatch.setattr(
        schema_consistency.subprocess, "check_output", lambda *a, **k: b""
    )


@pytest.mark.parametrize("dsn", **cases(LIBPQ_UNREADABLE))
def test_an_unreadable_database_url_is_refused_by_name(dsn, no_git, capsys):
    code = schema_consistency.main(
        ["--database-url", dsn, "--replay-database-url", READABLE]
    )

    out, err = capsys.readouterr()
    assert code == 1
    assert f"schema consistency failed: {OWNER_URL} {REFUSAL}" in err
    assert_withheld(out, err)

    with pytest.raises(ValueError) as caught:
        schema_consistency.run(dsn, READABLE)
    assert_refused_by_name(caught.value, OWNER_URL)


def test_an_unreadable_replay_url_is_refused_by_name(no_git, monkeypatch, capsys):
    """The first administrator is a stand-in, so the refusal is the replay
    cluster's, and the stand-in is still closed."""
    stand_in = stand_in_administrator(monkeypatch)

    replay = LIBPQ_UNREADABLE["raw-at-sign-then-colon"]
    code = schema_consistency.main(
        ["--database-url", STAND_IN, "--replay-database-url", replay]
    )

    out, err = capsys.readouterr()
    assert code == 1
    assert f"schema consistency failed: {REPLAY_URL} {REFUSAL}" in err
    assert_withheld(out, err)

    with pytest.raises(ValueError) as caught:
        schema_consistency.run(STAND_IN, replay)
    assert_refused_by_name(caught.value, REPLAY_URL)
    assert stand_in.closed
