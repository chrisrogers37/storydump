"""The two real processes the harness measures: the API exactly as the
Procfile runs it (`uvicorn src.api.app:app`, optionally `--workers N` for an
F5 run) and the target worker (`python -m src.worker`), both on the scratch
database, both speaking to the fake Telegram through
`TARGET_TELEGRAM_API_BASE`. Never `python -m src.main`."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import httpx

REPO_ROOT = Path(__file__).resolve().parents[3]
SECRET = "load-harness-secret"
BOT_TOKEN = "4242:load-harness-fake-token"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def process_env(
    *, database_url: str, fake_base: str, bot_username: str, workers: int = 1
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
        self, env: dict[str, str], *, port: Optional[int] = None, workers: int = 1
    ):
        self.port = port or free_port()
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
                if r.status_code == 200 and r.json().get("target_database"):
                    return r.json()
            except Exception as exc:  # noqa: BLE001 — still starting
                last = exc
            time.sleep(0.25)
        raise RuntimeError(f"API not ready within {timeout_s}s: {last!r}")

    def health(self) -> dict:
        return httpx.get(f"{self.base}/health", timeout=5.0).json()

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
