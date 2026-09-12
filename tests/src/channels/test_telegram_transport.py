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
    MediaTransient,
    MediaUnavailable,
    TelegramPaced,
    TelegramRefused,
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
    "workspace_id": "ws-1",
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


class TestMediaCardsStayHonest:
    """Review of #1259: only a DEFINITIVE refusal of the upload falls back —
    a lost answer, a 429 or a 5xx may have posted the card and propagate for
    the outbox's ambiguity policy; a dead token is a dead token; and a fetch
    that fails for a reason that is not the file's is loud and counted."""

    async def test_a_lost_upload_answer_propagates_without_a_text_fallback(self):
        calls = []

        def handler(request):
            calls.append(str(request.url).rsplit("/", 1)[1])
            raise httpx.ConnectError("boom")

        t = TelegramTransport(TOKEN, client=_client(handler), media_fetch=_fetch_ok)
        with pytest.raises(TelegramSendError):
            await t.for_chat("7")(MEDIA_ROW)
        assert calls == ["sendPhoto"], "one attempt, no re-upload, no text card"

    @pytest.mark.parametrize("code", [429, 500, 502])
    async def test_a_429_or_5xx_on_the_upload_is_ambiguous_not_a_refusal(self, code):
        calls = []

        def handler(request):
            calls.append(str(request.url).rsplit("/", 1)[1])
            return httpx.Response(
                code, json={"ok": False, "error_code": code, "description": "try later"}
            )

        t = TelegramTransport(TOKEN, client=_client(handler), media_fetch=_fetch_ok)
        with pytest.raises(TelegramSendError) as exc:
            await t.for_chat("7")(MEDIA_ROW)
        assert not isinstance(exc.value, TelegramRefused)
        assert calls == ["sendPhoto"]

    async def test_a_401_on_the_upload_is_the_dead_credential(self):
        def handler(request):
            return httpx.Response(
                401,
                json={"ok": False, "error_code": 401, "description": "Unauthorized"},
            )

        t = TelegramTransport(TOKEN, client=_client(handler), media_fetch=_fetch_ok)
        with pytest.raises(TelegramAuthDead):
            await t.for_chat("7")(MEDIA_ROW)
        assert t.auth_failures == 1

    async def test_a_media_block_for_another_workspace_is_refused_loudly(self):
        calls = []
        fetched = []

        async def fetch(media):
            fetched.append(media)
            return b"x", "a.jpg", "image/jpeg"

        def handler(request):
            calls.append(str(request.url).rsplit("/", 1)[1])
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 9}})

        t = TelegramTransport(TOKEN, client=_client(handler), media_fetch=fetch)
        row = dict(MEDIA_ROW, workspace_id="ws-OTHER")
        assert await t.for_chat("7")(row) == "9"
        assert fetched == [], "the grant is never asked for another tenant's file"
        assert calls == ["sendMessage"] and t.media_fetch_failures == 1

    async def test_the_files_own_failure_is_quiet_but_anything_else_is_counted(self):
        calls = []

        def handler(request):
            calls.append(str(request.url).rsplit("/", 1)[1])
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 3}})

        async def too_large(media):
            raise MediaUnavailable("11 bytes; the cap is 10")

        t = TelegramTransport(TOKEN, client=_client(handler), media_fetch=too_large)
        assert await t.for_chat("7")(MEDIA_ROW) == "3"
        assert t.media_fetch_failures == 0

        t2 = TelegramTransport(TOKEN, client=_client(handler), media_fetch=_fetch_fails)
        assert await t2.for_chat("7")(MEDIA_ROW) == "3"
        assert t2.media_fetch_failures == 1

    async def test_a_transient_fetch_failure_propagates_so_the_photo_is_resent_later(
        self,
    ):
        calls = []

        def handler(request):
            calls.append(str(request.url).rsplit("/", 1)[1])
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 3}})

        async def blip(media):
            raise MediaTransient("drive answered 503")

        t = TelegramTransport(TOKEN, client=_client(handler), media_fetch=blip)
        with pytest.raises(TelegramSendError):
            await t.for_chat("7")(MEDIA_ROW)
        assert calls == [] and t.media_fetch_failures == 0, "no text card for a blip"
        # The second attempt does not loop the card forever: the text card goes.
        assert await t.for_chat("7")(dict(MEDIA_ROW, attempts=2)) == "3"
        assert calls == ["sendMessage"] and t.media_fetch_failures == 0

    async def test_a_mime_telegram_will_not_take_as_a_photo_goes_as_a_document(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["body"] = _multipart(request)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 12}})

        async def fetch(media):
            return b"HEICBYTES", "shot.heic", "image/heic"

        row = dict(
            MEDIA_ROW,
            payload={
                **MEDIA_ROW["payload"],
                "media": {
                    **MEDIA_ROW["payload"]["media"],
                    "mime": "image/heic",
                    "file_name": "shot.heic",
                },
            },
        )
        t = TelegramTransport(TOKEN, client=_client(handler), media_fetch=fetch)
        assert await t.for_chat("7")(row) == "12"
        assert seen["url"].endswith("/sendDocument")
        assert b'name="document"; filename="shot.heic"' in seen["body"]

    async def test_a_413_is_a_definitive_refusal_and_falls_back(self):
        calls = []

        def handler(request):
            calls.append(str(request.url).rsplit("/", 1)[1])
            if calls[-1] == "sendPhoto":
                return httpx.Response(
                    413,
                    json={
                        "ok": False,
                        "error_code": 413,
                        "description": "Request Entity Too Large",
                    },
                )
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 6}})

        t = TelegramTransport(TOKEN, client=_client(handler), media_fetch=_fetch_ok)
        assert await t.for_chat("7")(MEDIA_ROW) == "6"
        assert calls == ["sendPhoto", "sendMessage"]

    async def test_a_row_that_cannot_vouch_for_its_workspace_is_refused_too(self):
        calls = []
        fetched = []

        async def fetch(media):
            fetched.append(media)
            return b"x", "a.jpg", "image/jpeg"

        def handler(request):
            calls.append(str(request.url).rsplit("/", 1)[1])
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 4}})

        t = TelegramTransport(TOKEN, client=_client(handler), media_fetch=fetch)
        row = {k: v for k, v in MEDIA_ROW.items() if k != "workspace_id"}
        assert await t.for_chat("7")(row) == "4"
        assert (
            fetched == [] and calls == ["sendMessage"] and t.media_fetch_failures == 1
        )


# ---------------------------------------------------------------------------
# The tap (phase 1 of the 2026-09-09 plan): answering a tap and editing a card.
# ---------------------------------------------------------------------------


def _ok(result=True):
    return httpx.Response(200, json={"ok": True, "result": result})


class TestAnsweringATap:
    async def test_answer_callback_posts_the_query_id_text_and_alert_flag(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["json"] = json.loads(request.content)
            return _ok()

        t = _transport(handler)
        assert await t.answer_callback("q1", "Skipped", show_alert=True) is True
        assert seen["url"].endswith("/answerCallbackQuery")
        assert seen["json"] == {
            "callback_query_id": "q1",
            "text": "Skipped",
            "show_alert": True,
        }

    async def test_a_failed_answer_is_false_never_a_raise(self):
        def handler(request):
            return httpx.Response(
                400,
                json={
                    "ok": False,
                    "error_code": 400,
                    "description": "Bad Request: query is too old",
                },
            )

        t = _transport(handler)
        assert await t.answer_callback("q1", "x") is False

    async def test_a_transport_failure_is_false_too(self):
        def handler(request):
            raise httpx.ConnectError("down")

        t = _transport(handler)
        assert await t.answer_callback("q1", "x") is False


class TestTheSupersedeRowEditsTheCard:
    """`prompt_supersede` rows edit the card in ONE call: the original header
    plus the outcome line with an empty keyboard in the same request (one
    Telegram message per tap per binding, 2026-09-12). A row without an
    outcome strips alone; a refused combined edit falls back to the
    type-agnostic strip — the call that must land."""

    async def test_one_caption_edit_carries_the_line_and_removes_the_keyboard(self):
        calls = []

        def handler(request):
            calls.append(
                (str(request.url).rsplit("/", 1)[-1], json.loads(request.content))
            )
            return _ok()

        t = _transport(handler)
        row = {
            "id": "ob-9",
            "kind": "prompt_supersede",
            "intent_id": "i1",
            "payload": {
                "v": 1,
                "supersedes_ref": "555",
                "outcome_text": "✅ Approved by Chris · 2026-09-09 14:14 UTC",
                "header": "📸 @brand\nSlot: 2026-09-09 14:00 UTC",
                "sent_as": "media",
            },
        }
        ref = await t.for_chat("-100")(row)
        assert ref == "555"
        assert [c[0] for c in calls] == ["editMessageCaption"], "one call, not two"
        assert (
            calls[0][1]["caption"]
            == "📸 @brand\nSlot: 2026-09-09 14:00 UTC\n✅ Approved by Chris · 2026-09-09 14:14 UTC"
        )
        assert calls[0][1]["reply_markup"] == {"inline_keyboard": []}
        assert calls[0][1]["message_id"] == 555

    async def test_one_text_edit_for_a_text_card(self):
        calls = []

        def handler(request):
            calls.append(
                (str(request.url).rsplit("/", 1)[-1], json.loads(request.content))
            )
            return _ok()

        t = _transport(handler)
        row = {
            "id": "ob-9",
            "kind": "prompt_supersede",
            "intent_id": "i1",
            "payload": {
                "v": 1,
                "supersedes_ref": "555",
                "outcome_text": "⏭️ Skipped",
                "header": "📸 f.jpg",
                "sent_as": "text",
            },
        }
        assert await t.for_chat("-100")(row) == "555"
        assert [c[0] for c in calls] == ["editMessageText"]
        assert calls[0][1]["text"] == "📸 f.jpg\n⏭️ Skipped"
        assert calls[0][1]["reply_markup"] == {"inline_keyboard": []}

    async def test_without_an_outcome_it_only_strips(self):
        calls = []

        def handler(request):
            calls.append(str(request.url).rsplit("/", 1)[-1])
            return _ok()

        t = _transport(handler)
        row = {
            "id": "ob-9",
            "kind": "prompt_supersede",
            "intent_id": "i1",
            "payload": {"v": 1, "supersedes_ref": "555"},
        }
        assert await t.for_chat("-100")(row) == "555"
        assert calls == ["editMessageReplyMarkup"]

    async def test_a_refused_outcome_edit_still_strips_the_keyboard(self):
        """The caption Telegram will not take must not leave the buttons: the
        type-agnostic strip is the fallback, and the row is sent."""
        calls = []

        def handler(request):
            name = str(request.url).rsplit("/", 1)[-1]
            calls.append(name)
            if name == "editMessageCaption":
                return httpx.Response(
                    400,
                    json={
                        "ok": False,
                        "error_code": 400,
                        "description": "Bad Request: there is no caption",
                    },
                )
            return _ok()

        t = _transport(handler)
        row = {
            "id": "ob-9",
            "kind": "prompt_supersede",
            "intent_id": "i1",
            "payload": {
                "v": 1,
                "supersedes_ref": "555",
                "outcome_text": "x",
                "header": "h",
                "sent_as": "media",
            },
        }
        assert await t.for_chat("-100")(row) == "555"
        assert calls == ["editMessageCaption", "editMessageReplyMarkup"]

    async def test_a_failed_outcome_edit_escapes_after_one_call(self):
        """Only a definitive refusal takes the strip fallback. A 5xx means
        the edit MAY have landed: it escapes to the outbox's own policy
        (ambiguous → resent under the prompt cap → failed) — a second call here would
        double-spend the pacing debit and could strip a card whose line
        already landed."""
        calls = []

        def handler(request):
            calls.append(str(request.url).rsplit("/", 1)[-1])
            return httpx.Response(
                502,
                json={"ok": False, "error_code": 502, "description": "Bad Gateway"},
            )

        t = _transport(handler)
        row = {
            "id": "ob-9",
            "kind": "prompt_supersede",
            "intent_id": "i1",
            "payload": {
                "v": 1,
                "supersedes_ref": "555",
                "outcome_text": "x",
                "header": "h",
                "sent_as": "text",
            },
        }
        with pytest.raises(TelegramSendError) as info:
            await t.for_chat("-100")(row)
        assert not isinstance(info.value, TelegramRefused)
        assert calls == ["editMessageText"], "no strip after a non-refusal"

    def test_a_refusal_is_the_outboxs_definitive_kind(self):
        """`settle` fails a `ChannelRefused` row outright — a 400 is Telegram
        saying the message as shaped will never land, not a lost answer."""
        from src.services.target.outbox import ChannelRefused

        assert issubclass(TelegramRefused, ChannelRefused)
        assert not issubclass(TelegramPaced, ChannelRefused)

    async def test_a_paced_outcome_edit_escapes_after_one_call(self):
        """A 429 on the combined edit is the sender's pacing signal, not a
        refusal: it escapes as `TelegramPaced` from the one call, so the
        outbox writes its hold instead of this branch hitting Telegram again."""
        calls = []

        def handler(request):
            calls.append(str(request.url).rsplit("/", 1)[-1])
            return httpx.Response(
                429,
                json={
                    "ok": False,
                    "error_code": 429,
                    "description": "Too Many Requests: retry after 7",
                    "parameters": {"retry_after": 7},
                },
            )

        t = _transport(handler)
        row = {
            "id": "ob-9",
            "kind": "prompt_supersede",
            "intent_id": "i1",
            "payload": {
                "v": 1,
                "supersedes_ref": "555",
                "outcome_text": "x",
                "header": "h",
                "sent_as": "media",
            },
        }
        with pytest.raises(TelegramPaced) as info:
            await t.for_chat("-100")(row)
        assert info.value.retry_after_s == 7.0
        assert calls == ["editMessageCaption"]

    async def test_not_modified_is_success_in_one_call(self):
        calls = []

        def handler(request):
            calls.append(str(request.url).rsplit("/", 1)[-1])
            return httpx.Response(
                400,
                json={
                    "ok": False,
                    "error_code": 400,
                    "description": "Bad Request: message is not modified",
                },
            )

        t = _transport(handler)
        row = {
            "id": "ob-9",
            "kind": "prompt_supersede",
            "intent_id": "i1",
            "payload": {
                "v": 1,
                "supersedes_ref": "555",
                "outcome_text": "x",
                "sent_as": "text",
            },
        }
        assert await t.for_chat("-100")(row) == "555"
        assert calls == ["editMessageText"], "an edit that already stands is done"

    async def test_a_failed_strip_fails_the_row(self):
        def handler(request):
            return httpx.Response(
                400,
                json={
                    "ok": False,
                    "error_code": 400,
                    "description": "Bad Request: message to edit not found",
                },
            )

        t = _transport(handler)
        row = {
            "id": "ob-9",
            "kind": "prompt_supersede",
            "intent_id": "i1",
            "payload": {"v": 1, "supersedes_ref": "555"},
        }
        with pytest.raises(TelegramRefused):
            await t.for_chat("-100")(row)

    async def test_a_refused_edit_and_a_refused_strip_fail_the_row(self):
        def handler(request):
            return httpx.Response(
                400,
                json={
                    "ok": False,
                    "error_code": 400,
                    "description": "Bad Request: message to edit not found",
                },
            )

        t = _transport(handler)
        row = {
            "id": "ob-9",
            "kind": "prompt_supersede",
            "intent_id": "i1",
            "payload": {
                "v": 1,
                "supersedes_ref": "555",
                "outcome_text": "x",
                "sent_as": "text",
            },
        }
        with pytest.raises(TelegramRefused):
            await t.for_chat("-100")(row)


class TestASendReceiptSaysHowTheCardWent:
    async def test_a_text_send_reports_text(self):
        def handler(request):
            return _ok({"message_id": 99})

        ref = await _transport(handler).for_chat("7")(ROW)
        assert ref == "99" and ref.sent_as == "text"

    async def test_a_media_send_reports_media(self):
        def handler(request):
            return _ok({"message_id": 77})

        t = TelegramTransport(TOKEN, client=_client(handler), media_fetch=_fetch_ok)
        ref = await t.for_chat("7")(MEDIA_ROW)
        assert ref == "77" and ref.sent_as == "media"


class TestTransportFromEnv:
    """`TARGET_TELEGRAM_API_BASE` (the load harness's fake Telegram): loopback
    only, never in production, and the floor admits the host on the fast
    calls too — derived from the transport's own policy."""

    def test_without_the_variable_it_is_the_real_telegram_under_the_default_floor(self):
        from src.channels.telegram_transport import _API_BASE, transport_from_env

        t = transport_from_env("1:t", {})
        assert t._api_base == _API_BASE
        assert t._policy.enforce_private_address_block is True
        assert t._fast.timeout_class == "fast" and t._fast.max_attempts == 1

    def test_a_loopback_base_is_admitted_on_every_call_class(self):
        from src.channels.telegram_transport import transport_from_env

        t = transport_from_env(
            "1:t", {"TARGET_TELEGRAM_API_BASE": "http://127.0.0.1:8123/"}
        )
        assert t._api_base == "http://127.0.0.1:8123"
        assert t._policy.allowed_hosts == frozenset({"127.0.0.1"}), "the fake alone"
        assert t._policy.enforce_private_address_block is False
        assert "127.0.0.1" in t._fast.allowed_hosts, "the answer and the strip too"

    @pytest.mark.parametrize(
        "base",
        [
            "http://10.0.0.5:80",
            "http://localhost:8123",  # a name, not an address: /etc/hosts decides
            "http://127.0.0.1@evil.example/",
            "http://localhost.evil.example/",
        ],
    )
    def test_anything_but_a_loopback_address_is_refused(self, base):
        from src.channels.telegram_transport import ApiBaseRefused, transport_from_env

        with pytest.raises(ApiBaseRefused, match="loopback"):
            transport_from_env("1:t", {"TARGET_TELEGRAM_API_BASE": base})

    def test_production_refuses_the_override_outright(self):
        from src.channels.telegram_transport import ApiBaseRefused, transport_from_env

        with pytest.raises(ApiBaseRefused, match="production"):
            transport_from_env(
                "1:t",
                {
                    "TARGET_TELEGRAM_API_BASE": "http://127.0.0.1:8123",
                    "RAILWAY_ENVIRONMENT_NAME": "production",
                },
            )


class TestAFloodLimitIsPacedNotFailed:
    """Phase 3a step 2: a 429 is neither a lost response (the message did
    not go) nor a fault of the row. It is `TelegramPaced` — the outbox's
    `ChannelPaced` — carrying Telegram's `retry_after` so the sender can
    write a durable hold and come back when told."""

    async def test_429_raises_the_paced_error_with_telegram_s_retry_after(self):
        from src.services.target.outbox import ChannelPaced

        t = _transport(
            lambda r: httpx.Response(
                429,
                json={
                    "ok": False,
                    "error_code": 429,
                    "description": "Too Many Requests: retry after 7",
                    "parameters": {"retry_after": 7},
                },
            )
        )
        with pytest.raises(TelegramPaced) as info:
            await t.send_text("555", "hi")
        assert isinstance(info.value, ChannelPaced)
        assert isinstance(info.value, TelegramSendError), (
            "a caller that catches the plain send error must still see it"
        )
        assert info.value.retry_after_s == 7.0
        assert info.value.scope == "chat", "a chat-addressed call names the chat"
        assert TOKEN not in str(info.value)

    async def test_a_429_without_retry_after_still_paces_for_a_default(self):
        t = _transport(
            lambda r: httpx.Response(
                429, json={"ok": False, "error_code": 429, "description": "flood"}
            )
        )
        with pytest.raises(TelegramPaced) as info:
            await t.send_text("555", "hi")
        assert info.value.retry_after_s == 5.0

    async def test_a_429_on_a_call_without_a_chat_is_global(self):
        t = _transport(
            lambda r: httpx.Response(
                429,
                json={
                    "ok": False,
                    "error_code": 429,
                    "description": "flood",
                    "parameters": {"retry_after": 2},
                },
            )
        )
        with pytest.raises(TelegramPaced) as info:
            await t.probe()
        assert info.value.scope == "global"
