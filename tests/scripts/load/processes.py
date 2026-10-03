"""The two real processes the harness measures: the API exactly as the
Procfile runs it (`uvicorn src.api.app:app`, optionally `--workers N` for an
F5 run) and the target worker (`python -m src.worker`), both on the scratch
database, both speaking to the fake Telegram through
`TARGET_TELEGRAM_API_BASE`. Never `python -m src.main`."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import httpx
import psycopg2

from src.services.target import sessions
from tests.scripts.load import free_port

REPO_ROOT = Path(__file__).resolve().parents[3]
SECRET = "load-harness-secret"
BOT_TOKEN = "4242:load-harness-fake-token"


def operator_session(database_url: str) -> tuple[str, str]:
    """A user and a live session for it, so the harness can read the API's
    operating details (`/api/v1/ops/health`): ``(user_id, session value)``.
    The INSERT is `sessions.issue`'s, spelled for psycopg2: keep the two in
    step."""
    value = sessions.new_token()
    conn = psycopg2.connect(database_url)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'migration'")
            cur.execute("INSERT INTO users DEFAULT VALUES RETURNING id")
            user_id = str(cur.fetchone()[0])
            cur.execute(
                "INSERT INTO session_tokens (user_id, token_hash, expires_at)"
                " VALUES (%s, %s, now() + make_interval(secs => %s))",
                (user_id, sessions.token_hash(value), sessions.SESSION_TTL_SECONDS),
            )
    finally:
        conn.close()
    return user_id, value


def process_env(
    *,
    database_url: str,
    fake_base: str,
    bot_username: str,
    workers: int = 1,
    operator_user_id: str = "",
) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "TARGET_DATABASE_URL": database_url,
            "TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN": SECRET,
            "TARGET_TELEGRAM_BOT_TOKEN": BOT_TOKEN,
            "TARGET_TELEGRAM_BOT_USERNAME": bot_username,
            "TARGET_TELEGRAM_API_BASE": fake_base,
            "TARGET_TELEGRAM_WEBHOOK_AUTOREGISTER": "0",
            "WEB_CONCURRENCY": str(workers),
            "OPS_USER_IDS": operator_user_id,
            "WORKER_LOG_LEVEL": "WARNING",
            "PYTHONPATH": str(REPO_ROOT),
            "PYTHONUNBUFFERED": "1",
        }
    )
    # Never let the harness inherit a production pointer or the runner's
    # per-test database switches.
    for key in ("DATABASE_URL", "RAILWAY_ENVIRONMENT_NAME"):
        env.pop(key, None)
    return env


class Api:
    def __init__(
        self,
        env: dict[str, str],
        *,
        port: Optional[int] = None,
        workers: int = 1,
        operator_session: str = "",
    ):
        self.port = port or free_port()
        self._operator = {"Authorization": f"Bearer {operator_session}"}
        cmd = [
            sys.executable,
            "-m",
            "uvicorn",
            "src.api.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
            "--log-level",
            "warning",
        ]
        if workers > 1:
            cmd += ["--workers", str(workers)]
        self._proc = subprocess.Popen(cmd, env=env, cwd=REPO_ROOT)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def wait_ready(self, timeout_s: float = 60.0) -> dict:
        deadline = time.monotonic() + timeout_s
        last: Optional[Exception] = None
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError(f"API exited early: {self._proc.returncode}")
            try:
                r = httpx.get(f"{self.base}/health", timeout=2.0)
                if r.status_code == 200:
                    return self.health()
            except Exception as exc:  # noqa: BLE001 — still starting
                last = exc
            time.sleep(0.25)
        raise RuntimeError(f"API not ready within {timeout_s}s: {last!r}")

    def health(self) -> dict:
        """The operating details, as the harness's operator reads them."""
        r = httpx.get(
            f"{self.base}/api/v1/ops/health", headers=self._operator, timeout=5.0
        )
        r.raise_for_status()
        return r.json()

    def stop(self) -> None:
        _stop(self._proc)


class Worker:
    def __init__(self, env: dict[str, str]):
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "src.worker"], env=env, cwd=REPO_ROOT
        )

    def alive(self) -> bool:
        return self._proc.poll() is None

    def stop(self) -> None:
        _stop(self._proc)


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
