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
from sqlalchemy.engine import make_url

from tests.unreadable_urls import (
    ENGINE_READABLE,
    ENGINE_UNREADABLE,
    PASSWORDS,
    REFUSAL,
    assert_exception_withholds,
    assert_withheld,
    cases,
)

#: Placeholders in Neon's shape. The password is the suite's planted secret,
#: so `leaks` can look for any run of it in what the refusal prints. The
#: unreadable forms are `tests/unreadable_urls.py`'s, below.
POOLED_HOST = "ep-xxx-pooler.region.aws.neon.tech"
DIRECT_HOST = "ep-xxx.region.aws.neon.tech"
CREDS = f"svc_worker:{MALFORMED_KEY}"

POOLED = "names a pooled database endpoint"
NO_HOST = "names no host"


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


@pytest.mark.parametrize("url", **cases(ENGINE_UNREADABLE))
def test_the_worker_refuses_a_url_it_cannot_read_and_never_echoes_it(
    url, ring_env, monkeypatch, capsys
):
    """Every form the API refuses to start on (`test_app_factory.py`), the
    worker refuses to boot on: one door reads the URL for both,
    `unit_of_work.engine_url_from_env`. What SQLAlchemy or asyncpg would raise
    can quote part of the password, so the refusal names the variable alone
    and carries nothing that quotes it."""
    import src.worker as worker

    _nothing_past_the_refusal_may_run(monkeypatch, worker)
    ring_env(key=Fernet.generate_key().decode())
    monkeypatch.setenv("TARGET_DATABASE_URL", url)
    with pytest.raises(SystemExit) as exc:
        worker.main()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert f"FATAL: TARGET_DATABASE_URL {REFUSAL}" in err and "Refusing to boot" in err
    assert_withheld(err)
    assert_exception_withholds(exc.value)


@pytest.mark.parametrize(
    "url",
    [
        f"postgresql://{CREDS}@{DIRECT_HOST}/db?sslmode=require",
        "postgresql://svc_worker@/storydump?host=/var/run/postgresql",
        # an abstract socket, its `@` encoded beside the user's
        "postgresql://svc_worker@/storydump?host=%40pgsock",
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


@pytest.mark.parametrize("form", list(ENGINE_READABLE))
def test_the_worker_builds_its_engine_on_the_password_its_writer_meant(
    form, ring_env, monkeypatch
):
    """A raw `/`, `?`, `#`, `[` or `]`, a compatibility character or an
    encoded `@` in a password is read as SQLAlchemy reads it, and the URL the
    engine gets carries it whole."""
    import src.worker as worker

    _nothing_past_the_refusal_may_run(monkeypatch, worker)
    handed = []

    def create_engine(url, **_kwargs):
        handed.append(url)
        raise _WentPastTheRefusal("the worker built its engine")

    monkeypatch.setattr(worker.unit_of_work, "create_engine", create_engine)
    ring_env(key=Fernet.generate_key().decode())
    monkeypatch.setenv("TARGET_DATABASE_URL", ENGINE_READABLE[form])
    with pytest.raises(_WentPastTheRefusal):
        worker.main()
    assert make_url(handed[0]).password == PASSWORDS[form]
