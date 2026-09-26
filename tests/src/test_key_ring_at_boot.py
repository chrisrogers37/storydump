"""Both roots build the credential key ring at startup and refuse to boot
without one (#1401).

Built lazily, a missing or malformed `ENCRYPTION_KEY` let either service pass
Railway's health check and fail later, one credential at a time: the worker at
its first token read, the API at the first connect callback — after the person
had granted access at Meta or Google. Refused at startup, the deploy fails its
check and the previous deploy keeps serving. What the doors do if a process
gets past this anyway is `tests/src/services/target/test_ring_unavailable.py`;
the ring is broken for real through `ring_env` (`tests/src/conftest.py`).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from src.services.target import oauth_states
from tests.src.conftest import MALFORMED_KEY, leaks

DATABASE_URL = "postgresql://nobody@localhost:1/nothing"

#: The remedy both roots name, because the tempting one — generate a key — boots
#: and then cannot read a single stored credential.
REMEDY = "the key the stored credentials were encrypted with"


class _WentPastTheRefusal(Exception):
    pass


def _nothing_past_the_refusal_may_run(monkeypatch, worker) -> None:
    """The tests' own safety, as the database-URL refusal's tests do it
    (`tests/src/test_legacy_settings_gone.py`): under the regression these
    exist to catch, `worker.main()` would build an engine and run a live
    worker inside pytest. Everything past the refusal stops the test instead —
    by raising, so the working-key control can see it was reached."""

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
        assert REMEDY in err

    def test_with_a_malformed_key_and_never_echoes_it(
        self, ring_env, monkeypatch, capsys
    ):
        import src.worker as worker

        _nothing_past_the_refusal_may_run(monkeypatch, worker)
        monkeypatch.setenv("TARGET_DATABASE_URL", DATABASE_URL)
        ring_env(key=MALFORMED_KEY)
        with pytest.raises(SystemExit) as exc:
            worker.main()
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "Invalid ENCRYPTION_KEY format" in err and REMEDY in err
        assert not leaks(err)

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

    def test_the_refusal_names_the_remedy_and_never_the_key(self, ring_env, caplog):
        from src.api.app import create_app
        from src.utils.logger import logger

        # The app's logger does not propagate to the root, where caplog listens.
        logger.addHandler(caplog.handler)
        try:
            ring_env(key=MALFORMED_KEY)
            with pytest.raises(oauth_states.RingUnavailable):
                with TestClient(create_app(env={})):
                    pass  # pragma: no cover — startup refuses
        finally:
            logger.removeHandler(caplog.handler)
        assert "Refusing to start" in caplog.text and REMEDY in caplog.text
        assert not leaks(caplog.text)

    def test_control_with_a_working_key_it_starts_and_answers(self, ring_env):
        from src.api.app import create_app

        ring_env(key=Fernet.generate_key().decode())
        with TestClient(create_app(env={})) as client:
            assert client.get("/health").status_code == 200


def _validate_env(tmp_path, **env) -> subprocess.CompletedProcess:
    """`make validate-env`, run from an empty directory so that neither make's
    `include .env` nor the settings' own `.env` can lend it a key; `python`
    is this interpreter and `src` imports from the checkout."""
    repo = Path(__file__).resolve().parents[2]
    run_env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("ENCRYPTION_KEY", "ENCRYPTION_KEYS")
    }
    run_env.update(
        PATH=f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}",
        PYTHONPATH=str(repo),
        **env,
    )
    return subprocess.run(
        ["make", "-f", str(repo / "Makefile"), "validate-env"],
        cwd=tmp_path,
        env=run_env,
        capture_output=True,
        text=True,
        timeout=120,
    )


class TestMakeValidateEnv:
    """It said "Configuration is valid" for a configuration both services now
    refuse to boot on: it builds the ring too, and fails when that fails."""

    def test_it_fails_where_both_roots_would_refuse(self, tmp_path):
        run = _validate_env(tmp_path)
        output = run.stdout + run.stderr
        assert run.returncode != 0, output
        assert "Configuration validation failed" in output
        assert "Configuration is valid" not in output

    def test_control_with_a_working_key_it_passes(self, tmp_path):
        run = _validate_env(tmp_path, ENCRYPTION_KEY=Fernet.generate_key().decode())
        assert run.returncode == 0, run.stdout + run.stderr
        assert "Configuration is valid" in run.stdout
