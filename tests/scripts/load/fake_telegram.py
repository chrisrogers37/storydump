"""A fake Telegram Bot API on this machine, for the load harness (S.1).

It answers the methods the two real processes call — `getMe`,
`getWebhookInfo`, `answerCallbackQuery`, `editMessageReplyMarkup`,
`editMessageCaption`, `editMessageText`, `sendMessage`/`sendPhoto`/`sendVideo`/
`sendDocument`, `setWebhook`/`deleteWebhook` — and RECORDS every call with the
monotonic time it arrived, so the harness can measure tap → answer and edit
landed end to end. A per-chat delay makes one chat slow (`one_slow_chat`).

Served by uvicorn on a background thread on a loopback port; the processes
reach it through `TARGET_TELEGRAM_API_BASE` (loopback only, never in
production — `telegram_transport.transport_from_env`).
"""

from __future__ import annotations

import asyncio
import itertools
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, Request

BOT_USERNAME = "storydump_load_fake_bot"


@dataclass
class Call:
    method: str
    at: float  # time.monotonic() on arrival
    chat_id: Optional[str]
    message_id: Optional[str]
    callback_query_id: Optional[str]
    payload: dict


@dataclass
class FakeTelegram:
    """The recorder. `chat_delays` maps a chat id (as Telegram sends it, a
    string) to seconds every call for that chat waits before it is answered;
    `callback_chat` lets a callback id name its chat (the harness mints ids
    as `<chat>:<n>`) so the delay applies to answers too."""

    chat_delays: dict[str, float] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)
    pending_update_count: int = 0
    max_connections: int = 10
    _ids: Any = field(default_factory=lambda: itertools.count(90_000))
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def app(self) -> FastAPI:
        app = FastAPI()

        @app.post("/bot{token}/{method}")
        async def call(token: str, method: str, request: Request):
            payload = await _body(request)
            chat = _chat_of(method, payload)
            cq = payload.get("callback_query_id")
            if method == "answerCallbackQuery" and cq and ":" in str(cq):
                chat = str(cq).split(":", 1)[0]
            call = Call(
                method=method,
                at=time.monotonic(),
                chat_id=None if chat is None else str(chat),
                message_id=(
                    None
                    if payload.get("message_id") is None
                    else str(payload.get("message_id"))
                ),
                callback_query_id=None if cq is None else str(cq),
                payload=payload,
            )
            with self._lock:
                self.calls.append(call)
            delay = self.chat_delays.get(call.chat_id or "", 0.0)
            if delay:
                await asyncio.sleep(delay)
            return {"ok": True, "result": self._result(method, payload)}

        return app

    def _result(self, method: str, payload: dict) -> Any:
        if method == "getMe":
            return {"id": 4242, "is_bot": True, "username": BOT_USERNAME}
        if method == "getWebhookInfo":
            return {
                "url": "",
                "pending_update_count": self.pending_update_count,
                "max_connections": self.max_connections,
                "allowed_updates": ["message", "callback_query"],
            }
        if method == "answerCallbackQuery":
            return True
        if method.startswith("edit"):
            return {"message_id": int(payload.get("message_id") or 0)}
        if method.startswith("send"):
            return {
                "message_id": next(self._ids),
                "chat": {"id": payload.get("chat_id")},
            }
        return True

    # -- what the harness reads ------------------------------------------------

    def snapshot(self) -> list[Call]:
        with self._lock:
            return list(self.calls)

    def answers(self) -> dict[str, float]:
        """callback_query_id → the first `answerCallbackQuery` arrival."""
        out: dict[str, float] = {}
        for c in self.snapshot():
            if c.method == "answerCallbackQuery" and c.callback_query_id:
                out.setdefault(c.callback_query_id, c.at)
        return out

    def edits(self, kinds: tuple[str, ...]) -> dict[tuple[str, str], float]:
        """(chat_id, message_id) → the first arrival of any of *kinds*."""
        out: dict[tuple[str, str], float] = {}
        for c in self.snapshot():
            if c.method in kinds and c.chat_id and c.message_id:
                out.setdefault((c.chat_id, c.message_id), c.at)
        return out

    def count(self, method: str) -> int:
        return sum(1 for c in self.snapshot() if c.method == method)


async def _body(request: Request) -> dict:
    ctype = request.headers.get("content-type", "")
    if "json" in ctype:
        data = await request.json()
        return dict(data) if isinstance(data, dict) else {}
    form = await request.form()
    return {k: v for k, v in form.items() if not hasattr(v, "filename")}


def _chat_of(method: str, payload: dict) -> Optional[str]:
    chat = payload.get("chat_id")
    return None if chat is None else str(chat)


class FakeServer:
    """uvicorn on a thread; `base` is what the processes are told."""

    def __init__(self, fake: FakeTelegram, *, host: str = "127.0.0.1", port: int = 0):
        self.fake = fake
        self.host = host
        self.port = port or _free_port()
        self._server = uvicorn.Server(
            uvicorn.Config(fake.app(), host=host, port=self.port, log_level="warning")
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    @property
    def base(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self, timeout_s: float = 10.0) -> "FakeServer":
        self._thread.start()
        deadline = time.monotonic() + timeout_s
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("fake Telegram did not start")
            time.sleep(0.05)
        return self

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
