"""The worker refuses to boot on a database URL whose host is pooled, absent
or unreadable.

The clock's election is a session-scoped advisory lock. Through a transaction
pooler (Neon's `-pooler` host) the server session outlives the worker and keeps
the lock, and after a handover no worker becomes the clock: no slots, no
recurring jobs. The worker reads the hosts the way SQLAlchemy's asyncpg dialect
hands them to asyncpg, and refuses a URL that names none, since asyncpg would
then take one from the environment. Refused before anything connects, the deploy
fails Railway's health check and the previous one keeps serving, as the
key-ring refusal does (`tests/src/test_key_ring_at_boot.py`).
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from tests.src.conftest import (
    MALFORMED_KEY,
    _nothing_past_the_refusal_may_run,
    _WentPastTheRefusal,
    leaks,
)

#: Placeholders in Neon's shape. The password is the suite's planted secret,
#: so `leaks` can look for any run of it in what the refusal prints; the two
#: unreadable forms carry an obviously fake one instead, checked by name.
POOLED_HOST = "ep-xxx-pooler.region.aws.neon.tech"
DIRECT_HOST = "ep-xxx.region.aws.neon.tech"
CREDS = f"svc_worker:{MALFORMED_KEY}"

POOLED = "names a pooled database endpoint"
NO_HOST = "names no host"
UNREADABLE = "is not a connection URL the worker can read"


@pytest.mark.parametrize(
    "url, refusal",
    [
        (f"postgresql://{CREDS}@{POOLED_HOST}/db?sslmode=require", POOLED),
        # a `host` query value replaces the authority's host in SQLAlchemy
        (f"postgresql://{CREDS}@{DIRECT_HOST}/db?host={POOLED_HOST}", POOLED),
        # every named host counts, even one the engine would not use
        (f"postgresql://{CREDS}@{POOLED_HOST}/db?host={DIRECT_HOST}", POOLED),
        # SQLAlchemy's two multi-host forms
        (f"postgresql://{CREDS}@/db?host={DIRECT_HOST},{POOLED_HOST}", POOLED),
        # of repeated `host=` values `asyncpg_url` keeps the last, the one the
        # engine would use, so that is the one judged: the pooled one here
        (
            f"postgresql://{CREDS}@/db?host={DIRECT_HOST}:5432&host={POOLED_HOST}:5432",
            POOLED,
        ),
        # no host: asyncpg would take PGHOST, set to the pooler below
        (f"postgresql://{CREDS}@/db", NO_HOST),
        # a key/value string is not a URL SQLAlchemy reads
        (
            f"host={POOLED_HOST} dbname=db user=svc_worker password=not-a-real-password",
            UNREADABLE,
        ),
        # a raw `?` inside the password makes `asyncpg_url` split the URL
        # there, and a traceback would quote what came before it
        (f"postgresql://svc_worker:not-a-real?password@{POOLED_HOST}/db", UNREADABLE),
    ],
)
def test_the_worker_refuses_by_name_and_never_echoes_the_url(
    url, refusal, ring_env, monkeypatch, capsys
):
    import src.worker as worker

    _nothing_past_the_refusal_may_run(monkeypatch, worker)
    ring_env(key=Fernet.generate_key().decode())
    monkeypatch.setenv("PGHOST", POOLED_HOST)
    monkeypatch.setenv("TARGET_DATABASE_URL", url)
    with pytest.raises(SystemExit) as exc:
        worker.main()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert f"TARGET_DATABASE_URL {refusal}" in err and "Refusing to boot" in err
    assert POOLED_HOST not in err, "the refusal printed the host"
    assert not leaks(err), "the refusal printed part of the password"
    assert "not-a-real" not in err, "the refusal printed part of the password"


@pytest.mark.parametrize(
    "url",
    [
        f"postgresql://{CREDS}@{DIRECT_HOST}/db?sslmode=require",
        "postgresql://svc_worker@/storydump?host=/var/run/postgresql",
    ],
)
def test_control_a_direct_host_goes_on_to_connect(url, ring_env, monkeypatch):
    """The refusal is the host's, not unconditional: a direct host, or a local
    socket named in the URL, goes on to the engine, whatever PGHOST says."""
    import src.worker as worker

    _nothing_past_the_refusal_may_run(monkeypatch, worker)
    ring_env(key=Fernet.generate_key().decode())
    monkeypatch.setenv("PGHOST", POOLED_HOST)
    monkeypatch.setenv("TARGET_DATABASE_URL", url)
    with pytest.raises(_WentPastTheRefusal):
        worker.main()
