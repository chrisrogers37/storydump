"""W2 — the Telegram outbox transport (#942 W2; deliver()'s injected seam).

Contract under test, in deliver()'s words: the transport takes the claimed
row, returns an external ref, raises to signal a lost response. On top of
that, the shitpost-alpha lesson as REQUIREMENTS (that fleet's outbound died
silently for an unknown period because a dead token had no loud surface):

- a DEAD CREDENTIAL is a distinct, named, observable state — `probe()` raises
  `TelegramAuthDead` at composition time, a mid-run 401/403 raises it per
  send and increments `auth_failures`, logged loudly ONCE rather than per row;
- the bot token NEVER appears in any exception text or log line, including
  errors that wrap the request URL (the URL embeds the token).

All HTTP is faked with httpx.MockTransport — the egress floor is exercised
for real (policy, host allowlist), the network is not.
"""

import httpx
import pytest

from src.channels.telegram_transport import (
    TelegramAuthDead,
    TelegramSendError,
    TelegramTransport,
)

TOKEN = "8675309:AAtestSECRETtokenVALUExyz"


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _transport(handler):
    return TelegramTransport(TOKEN, client=_client(handler))


ROW = {
    "id": "ob-1",
    "kind": "approval_prompt",
    "payload": {"v": 1, "text": "hi"},
    "attempts": 1,
    "intent_id": None,
}


class TestSending:
    async def test_a_send_returns_the_message_id_as_the_external_ref(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["json"] = request.read()
            return httpx.Response(
                200, json={"ok": True, "result": {"message_id": 4242}}
            )

        t = _transport(handler)
        send = t.for_chat("-100555")
        ref = await send(ROW)

        assert ref == "4242"
        assert "sendMessage" in seen["url"]
        body = seen["json"].decode()
        assert '"chat_id": "-100555"' in body or '"chat_id":"-100555"' in body
        assert "hi" in body

    async def test_reply_markup_rides_when_the_payload_carries_it(self):
        seen = {}

        def handler(request):
            seen["body"] = request.read().decode()
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

        t = _transport(handler)
        row = dict(
            ROW,
            payload={"v": 1, "text": "pick", "reply_markup": {"inline_keyboard": []}},
        )
        await t.for_chat("7")(row)
        assert "inline_keyboard" in seen["body"]

    async def test_a_payload_without_text_is_a_definitive_send_error(self):
        t = _transport(lambda r: httpx.Response(200, json={"ok": True}))
        with pytest.raises(TelegramSendError):
            await t.for_chat("7")(dict(ROW, payload={"v": 1}))


class TestFailureClassification:
    async def test_unauthorized_raises_the_named_dead_credential_error(self):
        def handler(request):
            return httpx.Response(
                401,
                json={"ok": False, "error_code": 401, "description": "Unauthorized"},
            )

        t = _transport(handler)
        with pytest.raises(TelegramAuthDead):
            await t.for_chat("7")(ROW)
        assert t.auth_failures == 1

    async def test_other_api_refusals_raise_the_plain_send_error(self):
        def handler(request):
            return httpx.Response(
                400,
                json={
                    "ok": False,
                    "error_code": 400,
                    "description": "Bad Request: chat not found",
                },
            )

        t = _transport(handler)
        with pytest.raises(TelegramSendError) as caught:
            await t.for_chat("7")(ROW)
        assert not isinstance(caught.value, TelegramAuthDead)
        assert t.auth_failures == 0

    async def test_the_token_never_appears_in_error_text(self):
        def handler(request):
            return httpx.Response(
                400, json={"ok": False, "error_code": 400, "description": "Bad Request"}
            )

        t = _transport(handler)
        with pytest.raises(TelegramSendError) as caught:
            await t.for_chat("7")(ROW)
        assert TOKEN not in str(caught.value)
        assert TOKEN not in repr(caught.value)


class TestProbe:
    async def test_probe_returns_the_bot_username_when_the_credential_lives(self):
        def handler(request):
            assert "getMe" in str(request.url)
            return httpx.Response(
                200, json={"ok": True, "result": {"username": "soak_bot"}}
            )

        t = _transport(handler)
        assert await t.probe() == "soak_bot"

    async def test_probe_raises_the_named_dead_credential_error_on_401(self):
        def handler(request):
            return httpx.Response(
                401,
                json={"ok": False, "error_code": 401, "description": "Unauthorized"},
            )

        t = _transport(handler)
        with pytest.raises(TelegramAuthDead) as caught:
            await t.probe()
        assert TOKEN not in str(caught.value)
