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

import json

import httpx
import pytest

from src.channels.telegram_transport import (
    TelegramAuthDead,
    TelegramSendError,
    TelegramTransport,
    TelegramChatGone,
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


class TestSendText:
    """The one-off sender the API's `/start` door uses for its acknowledgement
    (#1224 follow-up): a chat id and a text, no outbox row."""

    async def test_sends_the_text_to_the_chat_and_returns_the_message_id(self):
        captured = {}

        def handler(request):
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 99}})

        t = _transport(handler)
        assert await t.send_text("555", "Linked.") == "99"
        assert captured["url"].endswith("/sendMessage")
        assert captured["body"] == {"chat_id": "555", "text": "Linked."}

    async def test_for_chat_is_send_text_with_the_row_unpacked(self):
        sent = []

        def handler(request):
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

        t = _transport(handler)
        await t.for_chat("7")(dict(ROW, payload={"v": 1, "text": "hi"}))
        assert sent == [{"chat_id": "7", "text": "hi"}]


class TestAChatThatIsGoneIsNotADeadToken:
    """A kicked bot, a blocked bot or a deleted chat is a chat-level fact; only
    a 401 is the credential's own death (#1240 review)."""

    async def test_403_kicked_raises_chat_gone_not_auth_dead(self):
        def handler(request):
            return httpx.Response(
                403,
                json={
                    "ok": False,
                    "error_code": 403,
                    "description": "Forbidden: bot was kicked from the supergroup chat",
                },
            )

        t = _transport(handler)
        with pytest.raises(TelegramChatGone) as info:
            await t.send_text("-100777", "hi")
        assert info.value.migrate_to is None
        assert t.auth_failures == 0, (
            "a kicked bot must not count as a credential failure"
        )

    async def test_400_upgraded_to_supergroup_carries_the_new_chat_id(self):
        def handler(request):
            return httpx.Response(
                400,
                json={
                    "ok": False,
                    "error_code": 400,
                    "description": "Bad Request: group chat was upgraded to a supergroup chat",
                    "parameters": {"migrate_to_chat_id": -1009999},
                },
            )

        t = _transport(handler)
        with pytest.raises(TelegramChatGone) as info:
            await t.send_text("-777", "hi")
        assert info.value.migrate_to == "-1009999"

    async def test_401_is_still_the_dead_credential(self):
        def handler(request):
            return httpx.Response(
                401,
                json={"ok": False, "error_code": 401, "description": "Unauthorized"},
            )

        t = _transport(handler)
        with pytest.raises(TelegramAuthDead):
            await t.send_text("7", "hi")


MEDIA_ROW = {
    "id": "ob-2",
    "kind": "approval_prompt",
    "payload": {
        "v": 2,
        "text": "📸 sunset.jpg (image)\nSlot: 2026-08-21 14:30 America/New_York",
        "caption": "📸 @gatortails\nSlot: 2026-08-21 14:30 America/New_York",
        "reply_markup": {
            "inline_keyboard": [[{"text": "✅ Posted", "callback_data": "v1:posted:x"}]]
        },
        "media": {
            "workspace_id": "ws-1",
            "source_id": "src-1",
            "ref": "file-1",
            "kind": "image",
            "mime": "image/jpeg",
            "file_name": "sunset.jpg",
        },
    },
    "attempts": 1,
    "intent_id": "i-1",
}


async def _fetch_ok(media):
    return b"JPEGBYTES", media["file_name"], media["mime"]


async def _fetch_fails(media):
    raise RuntimeError("drive said no")


def _multipart(request):
    body = request.read()
    assert request.headers["content-type"].startswith("multipart/form-data"), (
        "a media card is a multipart upload"
    )
    return body


class TestMediaCards:
    """Legacy parity (owner, 2026-09-08): the card IS the photo or video, the
    account and slot its caption. The transport fetches the bytes through the
    injected `media_fetch` and uploads them; when the file cannot be fetched
    or Telegram refuses the upload, the text card is sent instead — a chat that
    is gone, or a dead token, still propagate."""

    async def test_an_image_card_is_a_sendphoto_upload_with_caption_and_keyboard(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["body"] = _multipart(request)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 77}})

        t = TelegramTransport(TOKEN, client=_client(handler), media_fetch=_fetch_ok)
        assert await t.for_chat("7")(MEDIA_ROW) == "77"
        assert seen["url"].endswith("/sendPhoto")
        body = seen["body"]
        assert b'name="photo"; filename="sunset.jpg"' in body
        assert b"Content-Type: image/jpeg" in body and b"JPEGBYTES" in body
        assert b'name="chat_id"' in body and b"\r\n7\r\n" in body
        assert b'name="caption"' in body and "@gatortails".encode() in body
        assert b'name="reply_markup"' in body and b"v1:posted:x" in body

    async def test_a_video_card_is_a_sendvideo_upload(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["body"] = _multipart(request)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 78}})

        async def fetch(media):
            return b"MP4BYTES", "clip.mp4", "video/mp4"

        row = dict(
            MEDIA_ROW,
            payload={
                **MEDIA_ROW["payload"],
                "media": {
                    **MEDIA_ROW["payload"]["media"],
                    "kind": "video",
                    "mime": "video/mp4",
                    "file_name": "clip.mp4",
                },
            },
        )
        t = TelegramTransport(TOKEN, client=_client(handler), media_fetch=fetch)
        assert await t.for_chat("7")(row) == "78"
        assert seen["url"].endswith("/sendVideo")
        assert b'name="video"; filename="clip.mp4"' in seen["body"]

    async def test_a_failed_fetch_falls_back_to_the_text_card(self):
        calls = []

        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 5}})

        t = TelegramTransport(TOKEN, client=_client(handler), media_fetch=_fetch_fails)
        assert await t.for_chat("7")(MEDIA_ROW) == "5"
        assert len(calls) == 1 and calls[0].endswith("/sendMessage")

    async def test_a_refused_upload_falls_back_to_the_text_card_once(self):
        calls = []

        def handler(request):
            calls.append(str(request.url))
            if calls[-1].endswith("/sendPhoto"):
                return httpx.Response(
                    400,
                    json={
                        "ok": False,
                        "error_code": 400,
                        "description": "Bad Request: PHOTO_INVALID_DIMENSIONS",
                    },
                )
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 6}})

        t = TelegramTransport(TOKEN, client=_client(handler), media_fetch=_fetch_ok)
        assert await t.for_chat("7")(MEDIA_ROW) == "6"
        assert [c.rsplit("/", 1)[1] for c in calls] == ["sendPhoto", "sendMessage"]

    async def test_a_chat_that_is_gone_propagates_without_a_fallback(self):
        calls = []

        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(
                403,
                json={
                    "ok": False,
                    "error_code": 403,
                    "description": "Forbidden: bot was kicked from the group chat",
                },
            )

        t = TelegramTransport(TOKEN, client=_client(handler), media_fetch=_fetch_ok)
        with pytest.raises(TelegramChatGone):
            await t.for_chat("7")(MEDIA_ROW)
        assert len(calls) == 1, "no text fallback into a chat that is gone"

    async def test_without_a_media_fetch_the_text_card_is_sent(self):
        calls = []

        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 8}})

        t = _transport(handler)
        assert await t.for_chat("7")(MEDIA_ROW) == "8"
        assert calls[0].endswith("/sendMessage")
