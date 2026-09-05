"""The Telegram outbox transport — deliver()'s injected seam, made loud (#942 W2).

Contract (deliver()'s words): takes the claimed outbox row, returns the
external message ref, raises to signal a lost or refused response — the
caller marks the row ambiguous and the outbox's own resolution machinery
takes it from there.

**A dead credential is a named, observable state, not a quiet one.** The
lesson is fresh and measured (shitpost-alpha, 2026-08-21: production outbound
dead for an unknown period because a rejected token had no loud surface —
zero events drained, so nothing alarmed):

- :meth:`TelegramTransport.probe` (`getMe`) runs at composition time; a 401/403
  raises :class:`TelegramAuthDead` and the worker starts WITHOUT the channel,
  parking `deliver_outbox` with the credential named in the reason — a
  recurring warning, not a one-time line.
- A mid-run 401/403 raises :class:`TelegramAuthDead` per send, increments
  `auth_failures` (surfaced in the worker status line), and logs loudly ONCE —
  a latch, so the log stays readable while the counter keeps counting.
- The bot token never appears in any exception text or log line. The request
  URL embeds it, so every raise path out of the HTTP layer is re-raised with
  the token redacted.

Payload contract (what W3's prompt production will emit): ``{"v": 1, "text":
str, "reply_markup": optional dict}``. A payload without text is a definitive
send error — there is nothing to send, and inventing a rendering would be a
quiet wrong message.

All HTTP goes through the egress floor (`api.telegram.org` is already in its
default allowlist). The outbox tick's open transaction is L.4's own design
(send-state and finalize commit together); the floor's UoW tripwire does not
cover poller sessions by construction, and nothing here flips any floor flag.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

import httpx

from src.services.target import egress
from src.services.target.egress import EgressPolicy
from src.services.target.outbox import DestinationGone

logger = logging.getLogger("channels.telegram")

_API_BASE = "https://api.telegram.org"


_CHAT_GONE_MARKERS = (
    "bot was kicked",
    "bot was blocked",
    "bot is not a member",
    "chat not found",
    "upgraded to a supergroup",
    "user is deactivated",
)


def _chat_gone(code, description: str) -> Optional[str]:
    """Telegram's chat-level refusals, by shape: a 403 is always about THIS
    chat for a bot (the credential's own death is a 401), and two 400s name a
    chat that is gone or moved. Anything else is a send error."""
    lowered = description.lower()
    if code == 403:
        return description or "forbidden"
    if code == 400 and any(marker in lowered for marker in _CHAT_GONE_MARKERS):
        return description
    return None


class TelegramSendError(Exception):
    """The transport could not produce an external ref for this row."""


class TelegramChatGone(DestinationGone, TelegramSendError):
    """The chat will not take messages: the bot was kicked or blocked, the
    chat was deleted, or a group became a supergroup (Telegram names the
    successor id in `parameters.migrate_to_chat_id`). A chat-level fact —
    never the credential's."""


class TelegramAuthDead(TelegramSendError):
    """Telegram rejected the credential itself (401/403) — the loud class."""


class TelegramTransport:
    """One bot credential; `for_chat` binds it to a binding's external ref."""

    def __init__(
        self,
        token: str,
        *,
        client: Optional[httpx.AsyncClient] = None,
        policy: Optional[EgressPolicy] = None,
        api_base: str = _API_BASE,
    ):
        self._token = token
        self._client = client or httpx.AsyncClient()
        self._policy = policy or EgressPolicy()
        self._api_base = api_base
        self.auth_failures = 0
        self._auth_dead_logged = False

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "<TOKEN>")

    async def _call(self, method: str, payload: dict) -> dict:
        url = f"{self._api_base}/bot{self._token}/{method}"
        try:
            response = await egress.request(
                self._client, "POST", url, policy=self._policy, json=payload
            )
        except Exception as exc:  # noqa: BLE001 — every path may embed the URL
            raise TelegramSendError(
                f"{method}: transport failure: {self._redact(str(exc))}"
            ) from None
        try:
            body = response.json()
        except json.JSONDecodeError:
            raise TelegramSendError(
                f"{method}: non-JSON response (HTTP {response.status_code})"
            ) from None
        if body.get("ok") is True:
            return body.get("result") or {}
        code = body.get("error_code", response.status_code)
        description = self._redact(str(body.get("description", "")))
        gone = _chat_gone(code, description)
        if gone is not None:
            migrate_to = (body.get("parameters") or {}).get("migrate_to_chat_id")
            raise TelegramChatGone(
                f"{method}: {code} {description}",
                migrate_to=None if migrate_to is None else str(migrate_to),
            )
        if code == 401:
            self.auth_failures += 1
            if not self._auth_dead_logged:
                self._auth_dead_logged = True
                logger.error(
                    "Telegram rejected the bot credential (%s on %s): %s — the"
                    " channel is DEAD until the token is replaced; further"
                    " failures count on auth_failures without re-logging",
                    code,
                    method,
                    description,
                )
            raise TelegramAuthDead(f"{method}: {code} {description}")
        raise TelegramSendError(f"{method}: {code} {description}")

    async def probe(self) -> str:
        """`getMe` — the composition-time liveness check. Returns the bot
        username; raises :class:`TelegramAuthDead` on a rejected credential."""
        result = await self._call("getMe", {})
        return str(result.get("username", ""))

    async def send_text(
        self, chat_id: str, text: str, *, reply_markup: Optional[dict] = None
    ) -> str:
        """One `sendMessage`: the text to the chat, the message id back. The
        outbox's per-binding sender and the `/start` door's acknowledgement
        (#1224 follow-up) are both this call."""
        message: dict = {"chat_id": chat_id, "text": text}
        if reply_markup:
            message["reply_markup"] = reply_markup
        result = await self._call("sendMessage", message)
        message_id = (result or {}).get("message_id")
        if message_id is None:
            raise TelegramSendError("sendMessage: ok response without a message_id")
        return str(message_id)

    def for_chat(self, external_ref: str):
        """The per-binding sender deliver() takes: row in, message ref out."""

        async def send(row: dict) -> str:
            payload = row.get("payload") or {}
            text_body = payload.get("text")
            if not text_body:
                raise TelegramSendError(
                    f"outbox row {row.get('id')}: payload carries no text —"
                    " nothing to send"
                )
            return await self.send_text(
                external_ref, text_body, reply_markup=payload.get("reply_markup")
            )

        return send

    async def aclose(self) -> None:
        await self._client.aclose()
