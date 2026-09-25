"""Both roots build the credential key ring at startup and refuse to boot
without one.

Built lazily, a missing or malformed `ENCRYPTION_KEY` let either service pass
Railway's health check and fail later, one credential at a time: the worker at
its first token read, the API at the first connect callback — after the person
had granted access at Meta or Google. Refused at startup, the deploy fails its
check and the previous deploy keeps serving. What the doors do if a process
gets past this anyway is `tests/src/services/target/test_ring_unavailable.py`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from src.services.target import oauth_states
from src.utils import encryption
from src.utils.encryption import TokenEncryption

#: Planted as a malformed key; the refusal must name the variable, not echo it.
SENTINEL = "not-a-fernet-key-SENTINEL-7f3a"

DATABASE_URL = "postgresql://nobody@localhost:1/nothing"


@pytest.fixture
def ring_env(monkeypatch):
    """Configure the ring for real: the settings `TokenEncryption` reads,
    with the singleton reset on the way in and on the way out."""

    def configure(*, key=None, keys=None):
        monkeypatch.setattr(
            encryption,
            "settings",
            SimpleNamespace(ENCRYPTION_KEY=key, ENCRYPTION_KEYS=keys),
        )
        TokenEncryption.reset()

    yield configure
    TokenEncryption.reset()


class _WentPastTheRefusal(Exception):
    pass


def _nothing_past_the_refusal_may_run(monkeypatch, worker) -> None:
    """The tests' own safety, as the database-URL refusal's tests do it
    (`tests/src/test_legacy_settings_gone.py`): under the regression these
    exist to catch, `worker.main()` would build an engine and run a live
    worker inside pytest. Everything past the refusal stops the test instead."""

    def refuse(*_args, **_kwargs):
        raise _WentPastTheRefusal("the worker went past its refusal and tried to boot")

    monkeypatch.setattr(worker.unit_of_work, "create_engine", refuse)
    monkeypatch.setattr(worker, "compose", refuse)
    monkeypatch.setattr(worker.asyncio, "run", refuse)


class TestTheWorkerRefusesToBoot:
    def test_without_a_key(self, ring_env, monkeypatch, capsys):
        import src.worker as worker

        _nothing_past_the_refusal_may_run(monkeypatch, worker)
        monkeypatch.setenv("TARGET_DATABASE_URL", DATABASE_URL)
        ring_env()
        with pytest.raises(SystemExit) as exc:
            worker.main()
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "ENCRYPTION_KEY" in err and "Refusing to boot" in err

    def test_with_a_malformed_key_and_never_echoes_it(
        self, ring_env, monkeypatch, capsys
    ):
        import src.worker as worker

        _nothing_past_the_refusal_may_run(monkeypatch, worker)
        monkeypatch.setenv("TARGET_DATABASE_URL", DATABASE_URL)
        ring_env(key=SENTINEL)
        with pytest.raises(SystemExit) as exc:
            worker.main()
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "ENCRYPTION_KEY" in err
        assert SENTINEL not in err

    def test_control_with_a_working_key_it_goes_on_to_connect(
        self, ring_env, monkeypatch
    ):
        """The refusal is the key's, not unconditional: with a working ring
        the worker reaches its engine."""
        import src.worker as worker

        _nothing_past_the_refusal_may_run(monkeypatch, worker)
        monkeypatch.setenv("TARGET_DATABASE_URL", DATABASE_URL)
        ring_env(key=Fernet.generate_key().decode())
        with pytest.raises(_WentPastTheRefusal):
            worker.main()


class TestTheApiRefusesToStart:
    def test_without_a_key_startup_fails_before_any_task(self, ring_env, monkeypatch):
        """Raised from the lifespan, uvicorn exits and the deploy fails its
        check. First, so a process that cannot encrypt starts nothing — the
        webhook registration included, which re-points the production bot."""
        from src.api import app as app_module

        started = []

        def register(app_, env):
            started.append("webhook")

            async def nothing():
                return None

            return nothing()

        monkeypatch.setattr(app_module, "_register_webhook", register)
        ring_env()
        app = app_module.create_app(env={})
        with pytest.raises(oauth_states.RingUnavailable, match="ENCRYPTION_KEY"):
            with TestClient(app):
                pass  # pragma: no cover — startup refuses
        assert started == []

    def test_a_malformed_key_is_refused_without_being_logged(self, ring_env, caplog):
        from src.api.app import create_app
        from src.utils.logger import logger

        # The app's logger does not propagate to the root, where caplog listens.
        logger.addHandler(caplog.handler)
        try:
            ring_env(key=SENTINEL)
            with pytest.raises(oauth_states.RingUnavailable):
                with TestClient(create_app(env={})):
                    pass  # pragma: no cover — startup refuses
        finally:
            logger.removeHandler(caplog.handler)
        assert "Refusing to start" in caplog.text
        assert SENTINEL not in caplog.text

    def test_control_with_a_working_key_it_starts_and_answers(self, ring_env):
        from src.api.app import create_app

        ring_env(key=Fernet.generate_key().decode())
        with TestClient(create_app(env={})) as client:
            assert client.get("/health").status_code == 200
