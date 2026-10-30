"""The Telegram outbox transport (#942; deliver()'s injected seam).

Contract under test, in deliver()'s words: the transport takes the claimed
row, returns an external ref, raises to signal a lost response. On top of
that, two REQUIREMENTS:

- a DEAD CREDENTIAL is a distinct, named, observable state — `probe()` raises
  `TelegramAuthDead` at composition time, a mid-run 401 raises it per send
  and increments `auth_failures`, logged loudly ONCE rather than per row;
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
    logger as transport_logger,
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
    async def test_a_dead_token_is_logged_once_while_every_send_is_counted(
        self, caplog
    ):
        """A 401 raises the named dead-credential error on every send. The
        latch: one ERROR line names it however many rows meet it, and
        `auth_failures` (the worker's status line) counts every one of them."""

        def handler(request):
            return httpx.Response(
                401,
                json={"ok": False, "error_code": 401, "description": "Unauthorized"},
            )

        t = _transport(handler)
        with caplog.at_level("ERROR", logger=transport_logger.name):
            for _ in range(3):
                with pytest.raises(TelegramAuthDead):
                    await t.for_chat("7")(ROW)
        logged = [r for r in caplog.records if r.name == transport_logger.name]
        assert [r.levelname for r in logged] == ["ERROR"]
        assert t.auth_failures == 3

    @pytest.mark.parametrize(
        "status, reply",
        [
            pytest.param(
                400,
                {
                    "json": {
                        "ok": False,
                        "error_code": 400,
                        "description": "Bad Request: chat not found",
                    }
                },
                id="a gone chat's 400",
            ),
            pytest.param(
                404,
                {"json": {"ok": False, "error_code": 404, "description": "Not Found"}},
                id="a 404 with Telegram's error body",
            ),
            pytest.param(
                401,
                {"text": "<html>401 Authorization Required</html>"},
                id="a 401 with no JSON",
            ),
        ],
    )
    async def test_other_api_refusals_raise_the_plain_send_error(self, status, reply):
        """Only Telegram's own 401 names a dead credential. Anything else, a 404
        with Telegram's error body and a 401 with none included, is a send
        error that `auth_failures` does not count."""
        t = _transport(lambda request: httpx.Response(status, **reply))
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
    (#1224): a chat id and a text, no outbox row."""

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

    @pytest.mark.parametrize(
        "lost", [httpx.ReadTimeout("no answer"), httpx.RemoteProtocolError("cut")]
    )
    async def test_a_lost_answer_is_not_sent_again_inside_the_call(self, lost):
        # The request reached Telegram and its answer did not come back: the
        # message may stand, so a second sendMessage would be a second copy.
        calls = []

        def handler(request):
            calls.append(str(request.url).rsplit("/", 1)[1])
            raise lost

        t = _transport(handler)
        with pytest.raises(TelegramSendError):
            await t.for_chat("7")(dict(ROW, payload={"v": 1, "text": "hi"}))
        assert calls == ["sendMessage"]


class TestACardSentSettled:
    """#1477: a card sent settled keeps the card's own text in its payload
    and carries its line in `outcome_text`, the outbox's record of the line a
    card carries. The send puts the line under the text, and the edit that
    brings a media card its picture puts it under the caption, as an edit
    does, so a later edit's header is the card without that line."""

    async def test_a_text_card_goes_out_with_its_line_under_it(self):
        sent = []

        def handler(request):
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

        t = _transport(handler)
        payload = {
            "v": 2,
            "text": "📸 f.jpg (image)",
            "outcome_text": "✅ Approved by YOLO",
        }
        await t.for_chat("7")(dict(ROW, payload=payload))
        assert sent == [
            {"chat_id": "7", "text": "📸 f.jpg (image)\n✅ Approved by YOLO"}
        ]

    async def test_a_media_cards_create_puts_its_line_under_its_text(self):
        calls = []
        t = TelegramTransport(TOKEN, client=_client(_bot(calls)), media_fetch=_fetch_ok)
        row = dict(
            MEDIA_ROW,
            payload={**MEDIA_ROW["payload"], "outcome_text": "✅ Posted by YOLO"},
        )
        assert await t.for_chat("7")(row) == "555"
        ((method, body),) = calls
        assert method == "sendMessage"
        assert json.loads(body)["text"] == (
            "📸 sunset.jpg (image)\nSlot: 2026-08-21 14:30 America/New_York\n"
            "✅ Posted by YOLO"
        )

    async def test_the_media_edit_puts_the_line_under_the_caption(self):
        """A card settled before its picture lands: the edit that brings the
        picture writes the line under the card's header, as the caption."""
        calls = []
        t = TelegramTransport(TOKEN, client=_client(_bot(calls)), media_fetch=_fetch_ok)
        row = dict(
            EDIT_ROW,
            payload={**EDIT_ROW["payload"], "outcome_text": "✅ Posted by YOLO"},
        )
        assert await t.for_chat("7")(row) == "555"
        ((method, body),) = calls
        assert method == "editMessageMedia"
        assert json.loads(_multipart_field(body, "media"))["caption"] == (
            "📸 @example.brand\nSlot: 2026-08-21 14:30 America/New_York\n"
            "✅ Posted by YOLO"
        )


class TestTheEditSeam:
    """#1477: the route's one edit — `text` rewrites a message and sets its
    keyboard (none unless `markup` says); `markup` alone edits the keyboard."""

    async def test_markup_alone_is_one_edit_of_the_keyboard(self):
        captured = []

        def handler(request):
            captured.append((str(request.url), json.loads(request.content)))
            return httpx.Response(200, json={"ok": True, "result": True})

        t = _transport(handler)
        menu = {"inline_keyboard": [[{"text": "‹ Back", "callback_data": "v1:back:x"}]]}
        await t.edit("555", "77", markup=menu)
        ((url, body),) = captured
        assert url.endswith("/editMessageReplyMarkup")
        assert body == {"chat_id": "555", "message_id": 77, "reply_markup": menu}

    async def test_text_edits_the_text_and_strips_the_keyboard_in_one_call(self):
        """The identity link's prompt, rewritten to its outcome with its
        buttons removed in the same call."""
        captured = []

        def handler(request):
            captured.append((str(request.url), json.loads(request.content)))
            return httpx.Response(200, json={"ok": True, "result": True})

        await _transport(handler).edit("555", "77", text="Cancelled.")
        ((url, body),) = captured
        assert url.endswith("/editMessageText")
        assert body == {
            "chat_id": "555",
            "message_id": 77,
            "text": "Cancelled.",
            "reply_markup": {"inline_keyboard": []},
        }

    @pytest.mark.parametrize(
        "change",
        [
            {"text": "Cancelled."},
            {
                "markup": {
                    "inline_keyboard": [
                        [{"text": "‹ Back", "callback_data": "v1:back:x"}]
                    ]
                }
            },
        ],
    )
    async def test_not_modified_is_success(self, change):
        """A repeated tap asks for the text or the keyboard the message
        already shows — a second Configure or Back, most often."""

        def handler(request):
            return httpx.Response(
                400,
                json={
                    "ok": False,
                    "error_code": 400,
                    "description": "Bad Request: message is not modified",
                },
            )

        await _transport(handler).edit("555", "77", **change)


class TestAChatThatIsGoneIsNotADeadToken:
    """A kicked bot, a blocked bot or a deleted chat is a chat-level fact; only
    a 401 is the credential's own death (#1240)."""

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
        "caption": "📸 @example.brand\nSlot: 2026-08-21 14:30 America/New_York",
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


def _multipart_field(body, name):
    """A text field of a multipart upload's body, decoded."""
    marker = f'name="{name}"\r\n\r\n'.encode()
    assert marker in body, f"the upload carries no {name!r} field"
    return body.split(marker, 1)[1].split(b"\r\n--", 1)[0].decode()


def _bot(calls, refusals=None):
    """A Bot API that records `(method, body)` into *calls* and answers every
    call ok with message 555, except the methods *refusals* maps to a
    `(status, description)` refusal."""
    refusals = refusals or {}

    def handler(request):
        name = str(request.url).rsplit("/", 1)[-1]
        calls.append((name, request.read()))
        if name in refusals:
            status, description = refusals[name]
            return httpx.Response(
                status,
                json={"ok": False, "error_code": status, "description": description},
            )
        return _ok({"message_id": 555})

    return handler


def _methods(calls):
    """The Bot API methods *calls* (from `_bot`) names, in order."""
    return [method for method, _ in calls]


def _levels(caplog):
    """The levels of what the transport logged, in order."""
    return [r.levelname for r in caplog.records if r.name == transport_logger.name]


# The edit that brings a media card its photo or video, as the sender claims
# it: the card's `media` block, `sent_as` saying the media is not yet on the
# message, and the story's keyboard.
EDIT_ROW = {
    "id": "ob-3",
    "kind": "prompt_supersede",
    "payload": {
        "v": 1,
        "supersedes_ref": "555",
        "header": MEDIA_ROW["payload"]["caption"],
        "sent_as": "text",
        "media": MEDIA_ROW["payload"]["media"],
        "reply_markup": {
            "inline_keyboard": [[{"text": "Configure", "callback_data": "v1:cfg:x"}]]
        },
    },
    "attempts": 1,
    "intent_id": "i-1",
    "workspace_id": "ws-1",
}


async def _edited(fetch, row=None):
    """The card's media edit run with *fetch* behind it: the receipt, the Bot
    API methods called and the transport."""
    calls = []
    t = TelegramTransport(TOKEN, client=_client(_bot(calls)), media_fetch=fetch)
    receipt = await t.for_chat("7")(row or EDIT_ROW)
    return receipt, _methods(calls), t


class TestMediaCards:
    """A media card is sent as its text; its photo or video lands on that
    message by the edit that follows (`editMessageMedia`), the card's only
    upload. The transport fetches the bytes through the injected `media_fetch`
    for that edit; when the file cannot be fetched or Telegram refuses the
    media, the text card stands and the edit brings the line and the keyboard
    alone — a chat that is gone, or a dead token, still propagate."""

    async def test_a_media_cards_create_is_its_text_and_fetches_nothing(self):
        """The card goes out as a small message answered at once; the fetch and
        the upload belong to the edit that follows."""
        calls, fetched = [], []

        async def fetch(media):
            fetched.append(media)
            return await _fetch_ok(media)

        t = TelegramTransport(TOKEN, client=_client(_bot(calls)), media_fetch=fetch)
        ref = await t.for_chat("7")(MEDIA_ROW)
        ((method, body),) = calls
        assert method == "sendMessage"
        assert json.loads(body)["text"] == MEDIA_ROW["payload"]["text"]
        assert ref.sent_as == "text" and fetched == []

    async def test_the_media_edit_is_one_editmessagemedia_with_caption_file_and_keyboard(
        self,
    ):
        calls = []
        t = TelegramTransport(TOKEN, client=_client(_bot(calls)), media_fetch=_fetch_ok)
        ref = await t.for_chat("7")(EDIT_ROW)
        ((method, body),) = calls
        assert method == "editMessageMedia"
        assert _multipart_field(body, "chat_id") == "7"
        assert _multipart_field(body, "message_id") == "555"
        assert json.loads(_multipart_field(body, "media")) == {
            "type": "photo",
            "media": "attach://photo",
            "caption": MEDIA_ROW["payload"]["caption"],
        }
        assert b'name="photo"; filename="sunset.jpg"' in body
        assert b"Content-Type: image/jpeg" in body and b"JPEGBYTES" in body
        markup = json.loads(_multipart_field(body, "reply_markup"))
        assert markup == EDIT_ROW["payload"]["reply_markup"]
        assert ref == "555" and ref.sent_as == "media"

    @pytest.mark.parametrize(
        "kind, mime, name, media_type",
        [
            pytest.param(
                "image", "image/png", "shot.png", "photo", id="a PNG is a photo"
            ),
            pytest.param(
                "video", "video/mp4", "clip.mp4", "video", id="an MP4 is a video"
            ),
            pytest.param(
                "image",
                "image/heic",
                "shot.heic",
                "document",
                id="a HEIC is a document",
            ),
        ],
    )
    async def test_a_video_rides_as_video_and_a_heic_as_document(
        self, kind, mime, name, media_type
    ):
        """Telegram takes a JPEG or PNG as a photo and an MP4 or QuickTime file
        as a video; anything else goes as a document, which still previews
        inline."""
        calls = []

        async def fetch(media):
            return b"BYTES", name, mime

        block = {
            **EDIT_ROW["payload"]["media"],
            "kind": kind,
            "mime": mime,
            "file_name": name,
        }
        row = dict(EDIT_ROW, payload={**EDIT_ROW["payload"], "media": block})
        t = TelegramTransport(TOKEN, client=_client(_bot(calls)), media_fetch=fetch)
        await t.for_chat("7")(row)
        ((method, body),) = calls
        assert method == "editMessageMedia"
        sent = json.loads(_multipart_field(body, "media"))
        assert (sent["type"], sent["media"]) == (media_type, f"attach://{media_type}")
        assert f'name="{media_type}"; filename="{name}"'.encode() in body

    async def test_the_media_edit_waits_out_its_own_class_not_the_shared_upload(
        self, monkeypatch
    ):
        """A card's photo is uploaded under the `card_upload` wait, long enough for
        Telegram to take in the file; the shared `upload` class keeps its 60 s."""
        from src.services.target.egress import TIMEOUT_CLASSES

        seen = {}

        async def call(method, payload=None, *, data=None, files=None, policy=None):
            seen["method"], seen["policy"] = method, policy
            return {"message_id": 7}

        def handler(request):
            raise AssertionError("a request was made past the stand-in `_call`")

        t = _transport(handler)
        monkeypatch.setattr(t, "_call", call)
        result = await t.edit_media(
            "-100555",
            "555",
            kind="image",
            content=b"jpeg",
            filename="card.jpg",
            mime="image/jpeg",
            caption="caption",
            reply_markup=None,
        )

        policy = seen["policy"]
        assert result == {"message_id": 7}
        assert seen["method"] == "editMessageMedia"
        assert policy.timeout_class == "card_upload"
        assert policy.timeout_s == TIMEOUT_CLASSES["card_upload"] == 120.0
        assert policy.total_budget_s > policy.timeout_s
        assert policy.retry_after_send is False
        assert TIMEOUT_CLASSES["upload"] == 60.0

    @pytest.mark.parametrize(
        "status, description",
        [
            pytest.param(
                400,
                "Bad Request: PHOTO_INVALID_DIMENSIONS",
                id="a file Telegram will not take",
            ),
            pytest.param(413, "Request Entity Too Large", id="a file too large"),
        ],
    )
    @pytest.mark.parametrize(
        "outcome, then",
        [
            pytest.param(None, "editMessageReplyMarkup", id="the keyboard alone"),
            pytest.param(
                "✅ Posted by YOLO", "editMessageText", id="the line and the keyboard"
            ),
        ],
    )
    async def test_a_refused_media_lands_the_keyboard_alone_and_says_the_card_stays_text(
        self, status, description, outcome, then, caplog
    ):
        """A definitive refusal of the media leaves the text card, which still
        gets its line and its keyboard; the receipt says `text`, so the sender
        records that no media is owed."""
        calls = []
        row = dict(EDIT_ROW)
        if outcome:
            row["payload"] = {**EDIT_ROW["payload"], "outcome_text": outcome}
        t = TelegramTransport(
            TOKEN,
            client=_client(_bot(calls, {"editMessageMedia": (status, description)})),
            media_fetch=_fetch_ok,
        )
        with caplog.at_level("WARNING", logger=transport_logger.name):
            ref = await t.for_chat("7")(row)
        assert _methods(calls) == ["editMessageMedia", then]
        landed = json.loads(calls[1][1])
        assert landed["reply_markup"] == EDIT_ROW["payload"]["reply_markup"]
        if outcome:
            assert landed["text"] == f"{MEDIA_ROW['payload']['caption']}\n{outcome}"
        assert ref == "555" and ref.sent_as == "text"
        assert _levels(caplog) == ["WARNING"]

    async def test_a_media_edit_that_already_stands_is_done(self):
        """A "message is not modified" answer says the picture is already on
        the message: the edit is confirmed as any other."""
        calls = []
        refusals = {"editMessageMedia": (400, "Bad Request: message is not modified")}
        t = TelegramTransport(
            TOKEN, client=_client(_bot(calls, refusals)), media_fetch=_fetch_ok
        )
        ref = await t.for_chat("7")(EDIT_ROW)
        assert _methods(calls) == ["editMessageMedia"]
        assert ref.sent_as == "media"

    async def test_a_gone_message_fails_the_media_edit_after_one_call(self):
        """A "message to edit not found" answer says the message is gone: no
        other call can land on it, so the row fails at once."""
        calls = []
        refusals = {"editMessageMedia": (400, "Bad Request: message to edit not found")}
        t = TelegramTransport(
            TOKEN, client=_client(_bot(calls, refusals)), media_fetch=_fetch_ok
        )
        with pytest.raises(TelegramRefused):
            await t.for_chat("7")(EDIT_ROW)
        assert _methods(calls) == ["editMessageMedia"], "no call after it"

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

    async def test_without_a_fetch_the_edit_brings_the_keyboard_and_the_card_stays_text(
        self,
    ):
        calls = []
        ref = await _transport(_bot(calls)).for_chat("7")(EDIT_ROW)
        assert _methods(calls) == ["editMessageReplyMarkup"]
        assert ref.sent_as == "text", "no media is owed on a card that cannot fetch it"


class TestMediaCardsStayHonest:
    """#1259: only a DEFINITIVE refusal of the media edit falls back — a lost
    answer, a flood wait or a 5xx may have put the picture on the message and
    propagate for the outbox's ambiguity policy, with no second upload inside
    the call; a dead token is a dead token; and a fetch that fails for a reason
    that is not the file's is loud and counted."""

    async def test_a_lost_media_answer_propagates_with_no_fallback_and_no_second_upload(
        self,
    ):
        calls = []

        def handler(request):
            calls.append(str(request.url).rsplit("/", 1)[1])
            raise httpx.ReadTimeout("no answer")

        t = TelegramTransport(TOKEN, client=_client(handler), media_fetch=_fetch_ok)
        with pytest.raises(TelegramSendError) as exc:
            await t.for_chat("7")(EDIT_ROW)
        assert not isinstance(exc.value, TelegramRefused)
        assert calls == ["editMessageMedia"], "one upload: no fallback, no re-upload"

    async def test_a_media_edit_that_never_left_is_attempted_again(self):
        # No connection was made, so the picture cannot be on the message: a
        # second attempt is the first one.
        calls = []

        def handler(request):
            calls.append(str(request.url).rsplit("/", 1)[1])
            if len(calls) == 1:
                raise httpx.ConnectError("refused")
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 5}})

        t = TelegramTransport(TOKEN, client=_client(handler), media_fetch=_fetch_ok)
        ref = await t.for_chat("7")(EDIT_ROW)
        assert calls == ["editMessageMedia", "editMessageMedia"]
        assert ref == "555" and ref.sent_as == "media"

    @pytest.mark.parametrize("code", [429, 500, 502])
    async def test_a_429_or_5xx_on_the_media_edit_is_ambiguous_not_a_refusal(
        self, code
    ):
        calls = []

        def handler(request):
            calls.append(str(request.url).rsplit("/", 1)[1])
            return httpx.Response(
                code, json={"ok": False, "error_code": code, "description": "try later"}
            )

        t = TelegramTransport(TOKEN, client=_client(handler), media_fetch=_fetch_ok)
        with pytest.raises(TelegramSendError) as exc:
            await t.for_chat("7")(EDIT_ROW)
        assert not isinstance(exc.value, TelegramRefused)
        assert calls == ["editMessageMedia"]

    async def test_a_401_on_the_media_edit_is_the_dead_credential(self):
        calls = []
        refusals = {"editMessageMedia": (401, "Unauthorized")}
        t = TelegramTransport(
            TOKEN, client=_client(_bot(calls, refusals)), media_fetch=_fetch_ok
        )
        with pytest.raises(TelegramAuthDead):
            await t.for_chat("7")(EDIT_ROW)
        assert _methods(calls) == ["editMessageMedia"] and t.auth_failures == 1

    async def test_a_chat_that_is_gone_propagates_from_the_media_edit(self):
        calls = []
        refusals = {
            "editMessageMedia": (403, "Forbidden: bot was kicked from the group chat")
        }
        t = TelegramTransport(
            TOKEN, client=_client(_bot(calls, refusals)), media_fetch=_fetch_ok
        )
        with pytest.raises(TelegramChatGone):
            await t.for_chat("7")(EDIT_ROW)
        assert _methods(calls) == ["editMessageMedia"], "no keyboard call into it"

    @pytest.mark.parametrize(
        "row",
        [
            pytest.param(
                dict(EDIT_ROW, workspace_id="ws-OTHER"),
                id="a block for another workspace",
            ),
            pytest.param(
                {k: v for k, v in EDIT_ROW.items() if k != "workspace_id"},
                id="a row that cannot vouch for its workspace",
            ),
        ],
    )
    async def test_a_media_block_the_row_cannot_vouch_for_is_refused_loudly(
        self, row, caplog
    ):
        fetched = []

        async def fetch(media):
            fetched.append(media)
            return b"x", "a.jpg", "image/jpeg"

        with caplog.at_level("WARNING", logger=transport_logger.name):
            ref, methods, t = await _edited(fetch, row)
        assert fetched == [], "the grant is never asked for another tenant's file"
        assert methods == ["editMessageReplyMarkup"] and ref.sent_as == "text"
        assert t.media_fetch_failures == 1
        assert _levels(caplog) == ["ERROR"]

    async def test_the_files_own_failure_is_quiet_but_anything_else_is_counted(
        self, caplog
    ):
        async def too_large(media):
            raise MediaUnavailable("11 bytes; the cap is 10")

        with caplog.at_level("WARNING", logger=transport_logger.name):
            ref, methods, t = await _edited(too_large)
        assert methods == ["editMessageReplyMarkup"] and ref.sent_as == "text"
        assert t.media_fetch_failures == 0
        assert _levels(caplog) == ["WARNING"]

        caplog.clear()
        with caplog.at_level("WARNING", logger=transport_logger.name):
            ref, methods, t = await _edited(_fetch_fails)
        assert methods == ["editMessageReplyMarkup"] and ref.sent_as == "text"
        assert t.media_fetch_failures == 1
        assert _levels(caplog) == ["ERROR"]

    @pytest.mark.parametrize("attempts", [1, 3])
    async def test_a_transient_fetch_failure_leaves_the_card_text_at_once(
        self, attempts, caplog
    ):
        """A blip costs the card its picture, not its keyboard: the edit lands
        now, on any attempt, rather than raising to be resent with a media the
        outbox would give up anyway."""

        async def blip(media):
            raise MediaTransient("drive answered 503")

        with caplog.at_level("WARNING", logger=transport_logger.name):
            ref, methods, t = await _edited(blip, dict(EDIT_ROW, attempts=attempts))
        assert methods == ["editMessageReplyMarkup"] and ref.sent_as == "text"
        assert t.media_fetch_failures == 0
        assert _levels(caplog) == ["WARNING"]

    @pytest.mark.parametrize("attempts", [1, 3])
    @pytest.mark.parametrize("preparation_failed", [False, True])
    async def test_a_media_not_admitted_leaves_the_card_text_at_once(
        self, attempts, preparation_failed, caplog
    ):
        from src.services.target.outbox import MediaAdmissionNotSent

        async def fetch(media):
            raise MediaAdmissionNotSent(
                "past_posts_revalidation_required",
                preparation_failed=preparation_failed,
            )

        with caplog.at_level("WARNING", logger=transport_logger.name):
            ref, methods, t = await _edited(fetch, dict(EDIT_ROW, attempts=attempts))
        assert methods == ["editMessageReplyMarkup"] and ref.sent_as == "text"
        assert t.media_fetch_failures == int(preparation_failed)
        expected = ["ERROR", "WARNING"] if preparation_failed else ["WARNING"]
        assert _levels(caplog) == expected


# ---------------------------------------------------------------------------
# The tap: answering a tap and editing a card.
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
    Telegram message per tap per binding). A row without an outcome strips
    alone; a refused combined edit falls back to the type-agnostic strip —
    the call that must land — unless the refusal says the message is gone, and
    after a refusal that says the message is not the type the record says (no
    text to edit in a photo, no caption on text) the other edit is tried
    once."""

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
                "outcome_text": "✅ Approved by Sam · 2026-09-09 14:14 UTC",
                "header": "📸 @brand\nSlot: 2026-09-09 14:00 UTC",
                "sent_as": "media",
            },
        }
        ref = await t.for_chat("-100")(row)
        assert ref == "555"
        assert [c[0] for c in calls] == ["editMessageCaption"], "one call, not two"
        assert (
            calls[0][1]["caption"]
            == "📸 @brand\nSlot: 2026-09-09 14:00 UTC\n✅ Approved by Sam · 2026-09-09 14:14 UTC"
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
                        "description": "Bad Request: message caption is too long",
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

    @pytest.mark.parametrize(
        "sent_as, method",
        [("text", "editMessageText"), ("media", "editMessageCaption")],
    )
    async def test_a_deleted_message_fails_the_edit_after_one_call(
        self, sent_as, method
    ):
        """A combined edit refused with "message to edit not found" says the
        message is gone: no call can land on it, so the keyboard-only strip is
        not made and the row fails at once. Any other 400 takes the strip
        (`test_a_refused_outcome_edit_still_strips_the_keyboard`)."""
        calls = []

        def handler(request):
            calls.append(str(request.url).rsplit("/", 1)[-1])
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
                "header": "h",
                "sent_as": sent_as,
            },
        }
        with pytest.raises(TelegramRefused):
            await t.for_chat("-100")(row)
        assert calls == [method], "one call, and no editMessageReplyMarkup after it"

    async def test_a_failed_outcome_edit_escapes_after_one_call(self):
        """Only a definitive refusal takes the strip fallback. A 5xx means
        the edit MAY have landed: it escapes to the outbox's own policy
        (ambiguous → resent until confirmed) — a second call here would
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

    async def test_a_payload_keyboard_rides_the_edit_instead_of_the_empty_one(self):
        """A `review_required` card keeps buttons — the three resolutions —
        so the restate's supersede row carries its own `reply_markup`, and
        the one edit sends THAT instead of the empty keyboard."""
        calls = []

        def handler(request):
            calls.append(
                (str(request.url).rsplit("/", 1)[-1], json.loads(request.content))
            )
            return _ok()

        keyboard = {
            "inline_keyboard": [
                [{"text": "🔁 Post again", "callback_data": "v1:retry:x"}],
            ]
        }
        t = _transport(handler)
        row = {
            "id": "ob-9",
            "kind": "prompt_supersede",
            "intent_id": "i1",
            "payload": {
                "v": 1,
                "supersedes_ref": "555",
                "outcome_text": "👀 Needs review · 12:00 UTC",
                "header": "📸 @brand",
                "sent_as": "media",
                "reply_markup": keyboard,
            },
        }
        assert await t.for_chat("-100")(row) == "555"
        assert [c[0] for c in calls] == ["editMessageCaption"]
        assert calls[0][1]["reply_markup"] == keyboard

    async def test_the_fallback_strip_sets_the_payload_keyboard_too(self):
        calls = []

        def handler(request):
            name = str(request.url).rsplit("/", 1)[-1]
            calls.append((name, json.loads(request.content)))
            if name == "editMessageCaption":
                return httpx.Response(
                    400,
                    json={
                        "ok": False,
                        "error_code": 400,
                        "description": "Bad Request: message caption is too long",
                    },
                )
            return _ok()

        keyboard = {
            "inline_keyboard": [[{"text": "x", "callback_data": "v1:giveup:x"}]]
        }
        t = _transport(handler)
        row = {
            "id": "ob-9",
            "kind": "prompt_supersede",
            "intent_id": "i1",
            "payload": {
                "v": 1,
                "supersedes_ref": "555",
                "outcome_text": "x",
                "sent_as": "media",
                "reply_markup": keyboard,
            },
        }
        assert await t.for_chat("-100")(row) == "555"
        assert [c[0] for c in calls] == ["editMessageCaption", "editMessageReplyMarkup"]
        assert calls[1][1]["reply_markup"] == keyboard

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
        calls = []

        def handler(request):
            calls.append(str(request.url).rsplit("/", 1)[-1])
            return httpx.Response(
                400,
                json={
                    "ok": False,
                    "error_code": 400,
                    "description": "Bad Request: message can't be edited",
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
        assert calls == ["editMessageText", "editMessageReplyMarkup"], (
            "the strip was tried after the edit, and its refusal fails the row"
        )

    @staticmethod
    def _line_row(sent_as):
        """A supersede row that writes a line under the card's header, the
        card's record saying the message stands as *sent_as*."""
        return {
            "id": "ob-9",
            "kind": "prompt_supersede",
            "intent_id": "i1",
            "payload": {
                "v": 1,
                "supersedes_ref": "555",
                "outcome_text": "✅ Approved by Sam",
                "header": "📸 @brand\nSlot: 2026-09-09 14:00 UTC",
                "sent_as": sent_as,
                "reply_markup": {
                    "inline_keyboard": [[{"text": "x", "callback_data": "v1:cfg:x"}]]
                },
            },
        }

    async def test_a_text_edit_refused_because_the_message_is_a_photo_is_tried_as_a_caption(
        self,
    ):
        """The record says text but the message is a photo or video (an upload
        whose answer was lost landed after all): Telegram says there is no text
        to edit, so the line is written as the caption, once, and the receipt
        says the message is media."""
        calls = []
        refusals = {
            "editMessageText": (
                400,
                "Bad Request: there is no text in the message to edit",
            )
        }
        row = self._line_row("text")
        ref = await _transport(_bot(calls, refusals)).for_chat("-100")(row)
        assert _methods(calls) == ["editMessageText", "editMessageCaption"]
        retry = json.loads(calls[1][1])
        assert retry["caption"] == (
            "📸 @brand\nSlot: 2026-09-09 14:00 UTC\n✅ Approved by Sam"
        )
        assert retry["reply_markup"] == row["payload"]["reply_markup"]
        assert ref == "555" and ref.sent_as == "media"

    async def test_a_caption_edit_refused_because_the_message_is_text_is_tried_as_text(
        self,
    ):
        """The record says media but the message is text: Telegram says there
        is no caption to edit, so the line is written as the text, once, and
        the receipt says the message is text."""
        calls = []
        refusals = {
            "editMessageCaption": (
                400,
                "Bad Request: there is no caption in the message to edit",
            )
        }
        row = self._line_row("media")
        ref = await _transport(_bot(calls, refusals)).for_chat("-100")(row)
        assert _methods(calls) == ["editMessageCaption", "editMessageText"]
        retry = json.loads(calls[1][1])
        assert retry["text"] == (
            "📸 @brand\nSlot: 2026-09-09 14:00 UTC\n✅ Approved by Sam"
        )
        assert retry["reply_markup"] == row["payload"]["reply_markup"]
        assert ref == "555" and ref.sent_as == "text"

    async def test_a_refused_retry_still_strips_the_keyboard(self, caplog):
        """The other edit is refused too (a caption that is too long, say): the
        keyboard still lands by the type-agnostic strip, and the receipt says
        an edit, as it does for any combined edit that was refused."""
        calls = []
        refusals = {
            "editMessageText": (
                400,
                "Bad Request: there is no text in the message to edit",
            ),
            "editMessageCaption": (400, "Bad Request: message caption is too long"),
        }
        row = self._line_row("text")
        with caplog.at_level("WARNING", logger=transport_logger.name):
            ref = await _transport(_bot(calls, refusals)).for_chat("-100")(row)
        assert _methods(calls) == [
            "editMessageText",
            "editMessageCaption",
            "editMessageReplyMarkup",
        ]
        assert json.loads(calls[2][1])["reply_markup"] == row["payload"]["reply_markup"]
        assert ref == "555" and ref.sent_as == "edit"
        assert _levels(caplog) == ["WARNING"]

    async def test_a_retry_that_finds_the_message_gone_fails_the_edit_with_no_strip(
        self,
    ):
        calls = []
        refusals = {
            "editMessageText": (
                400,
                "Bad Request: there is no text in the message to edit",
            ),
            "editMessageCaption": (400, "Bad Request: message to edit not found"),
        }
        with pytest.raises(TelegramRefused):
            await _transport(_bot(calls, refusals)).for_chat("-100")(
                self._line_row("text")
            )
        assert _methods(calls) == ["editMessageText", "editMessageCaption"], (
            "no strip into a message that is gone"
        )

    async def test_a_retry_that_already_stands_is_done(self):
        """A "message is not modified" answer to the other edit is success, as
        it is for any edit: the message stands as the retry says."""
        calls = []
        refusals = {
            "editMessageText": (
                400,
                "Bad Request: there is no text in the message to edit",
            ),
            "editMessageCaption": (400, "Bad Request: message is not modified"),
        }
        ref = await _transport(_bot(calls, refusals)).for_chat("-100")(
            self._line_row("text")
        )
        assert _methods(calls) == ["editMessageText", "editMessageCaption"]
        assert ref.sent_as == "media"


class TestASendReceiptSaysHowTheCardWent:
    async def test_a_text_send_reports_text(self):
        def handler(request):
            return _ok({"message_id": 99})

        ref = await _transport(handler).for_chat("7")(ROW)
        assert ref == "99" and ref.sent_as == "text"

    async def test_a_media_cards_create_reports_text_and_its_media_edit_reports_media(
        self,
    ):
        t = TelegramTransport(TOKEN, client=_client(_bot([])), media_fetch=_fetch_ok)
        create = await t.for_chat("7")(MEDIA_ROW)
        assert create == "555" and create.sent_as == "text"
        edit = await t.for_chat("7")(EDIT_ROW)
        assert edit == "555" and edit.sent_as == "media"
        line = {**EDIT_ROW["payload"], "sent_as": "media", "outcome_text": "x"}
        line_edit = await t.for_chat("7")(dict(EDIT_ROW, payload=line))
        assert line_edit.sent_as == "edit", "a line or keyboard edit is neither"


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
    """A 429 is neither a lost response (the message did
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


class TestAFloodWaitSentAsA400IsPacedToo:
    """Telegram sometimes re-codes its flood wait as a 400 whose description
    names it ("Too Many Requests: retry after N") with no `retry_after`
    parameter. It is the same wait as a 429: the message did not go, so it is
    `TelegramPaced`, never a refusal that loses a card's media or fails a row,
    and the call is made once, with no fallback into the flood."""

    FLOOD = "Bad Request: too Many Requests: retry after 8"

    @pytest.mark.parametrize(
        "row, method",
        [
            pytest.param(ROW, "sendMessage", id="a message"),
            pytest.param(EDIT_ROW, "editMessageMedia", id="a card's media edit"),
            pytest.param(
                {
                    "id": "ob-9",
                    "kind": "prompt_supersede",
                    "intent_id": "i1",
                    "payload": {"v": 1, "supersedes_ref": "555"},
                },
                "editMessageReplyMarkup",
                id="a keyboard edit",
            ),
        ],
    )
    async def test_it_is_paced_after_one_call(self, row, method):
        calls = []
        t = TelegramTransport(
            TOKEN,
            client=_client(_bot(calls, {method: (400, self.FLOOD)})),
            media_fetch=_fetch_ok,
        )
        with pytest.raises(TelegramPaced) as info:
            await t.for_chat("7")(row)
        assert info.value.retry_after_s == 8.0
        assert info.value.code == 400
        assert info.value.scope == "chat"
        assert _methods(calls) == [method], "one call: no fallback into the flood"

    async def test_any_other_400_is_still_a_refusal(self):
        calls = []
        refusals = {"sendMessage": (400, "Bad Request: message is too long")}
        t = _transport(_bot(calls, refusals))
        with pytest.raises(TelegramRefused):
            await t.send_text("555", "hi")
        assert _methods(calls) == ["sendMessage"]


@pytest.mark.parametrize("attempts", [1, 3])
async def test_a_past_posts_cards_text_goes_without_a_fetch_and_its_media_edit_stays_text_when_admission_is_refused(
    attempts, caplog
):
    from src.services.target.outbox import MediaAdmissionNotSent

    calls, fetched = [], []

    async def fetch(media):
        fetched.append(media)
        raise MediaAdmissionNotSent("past_posts_revalidation_required")

    block = {**MEDIA_ROW["payload"]["media"], "intent_id": MEDIA_ROW["intent_id"]}
    create = {
        **MEDIA_ROW,
        "attempts": attempts,
        "payload": {**MEDIA_ROW["payload"], "media": block},
    }
    edit = {
        **EDIT_ROW,
        "attempts": attempts,
        "payload": {**EDIT_ROW["payload"], "media": block},
    }
    transport = TelegramTransport(TOKEN, client=_client(_bot(calls)), media_fetch=fetch)
    card = await transport.for_chat("7")(create)
    assert card.sent_as == "text" and fetched == [], "the create fetches nothing"
    with caplog.at_level("WARNING", logger=transport_logger.name):
        receipt = await transport.for_chat("7")(edit)
    assert _methods(calls) == ["sendMessage", "editMessageReplyMarkup"]
    assert receipt.sent_as == "text" and transport.media_fetch_failures == 0
    assert _levels(caplog) == ["WARNING"]


async def test_past_posts_media_intent_mismatch_refuses_before_fetch_or_send():
    from src.services.target.outbox import MediaAdmissionNotSent

    called = []

    async def fetch(media):
        called.append("fetch")
        return b"JPEG", "synthetic.jpg", "image/jpeg"

    def handler(request):
        called.append("send")
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 77}})

    transport = TelegramTransport(TOKEN, client=_client(handler), media_fetch=fetch)
    row = {
        **MEDIA_ROW,
        "workspace_id": "ws-1",
        "payload": {
            **MEDIA_ROW["payload"],
            "media": {**MEDIA_ROW["payload"]["media"], "intent_id": "different-intent"},
        },
    }
    with pytest.raises(MediaAdmissionNotSent):
        await transport.for_chat("7")(row)
    assert called == []


async def test_unexpected_past_posts_preparation_failure_is_no_send_and_counted(
    monkeypatch, caplog
):
    from src import worker
    from tests.src.services.target.test_media_adapters import _source_factory

    _source_factory(
        monkeypatch, {"provider": "instagram_past_posts", "config": {"v": 1}}
    )
    calls = []

    async def selected(**kwargs):
        raise RuntimeError("synthetic-secret-never-log")

    fetch = worker._card_media_fetch(object(), {}, fetch_selected=selected)
    transport = TelegramTransport(TOKEN, client=_client(_bot(calls)), media_fetch=fetch)
    row = {
        **EDIT_ROW,
        "payload": {
            **EDIT_ROW["payload"],
            "media": {
                **EDIT_ROW["payload"]["media"],
                "intent_id": EDIT_ROW["intent_id"],
            },
        },
    }
    with caplog.at_level("WARNING", logger=transport_logger.name):
        receipt = await transport.for_chat("7")(row)
    assert _methods(calls) == ["editMessageReplyMarkup"]
    assert receipt.sent_as == "text" and transport.media_fetch_failures == 1
    assert _levels(caplog) == ["ERROR", "WARNING"]
    assert "synthetic-secret" not in caplog.text


@pytest.mark.parametrize("fault", ["foreign_workspace", "unsupported_kind", "no_fetch"])
async def test_past_posts_card_setup_refusal_never_uses_actionable_text(fault):
    from src.services.target.outbox import MediaAdmissionNotSent

    sent = []

    async def fetch(media):
        raise AssertionError("setup refusal must precede fetch")

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 77}})

    media = {**MEDIA_ROW["payload"]["media"], "intent_id": MEDIA_ROW["intent_id"]}
    if fault == "foreign_workspace":
        media["workspace_id"] = "foreign"
    if fault == "unsupported_kind":
        media["kind"] = "other"
    transport = TelegramTransport(
        TOKEN,
        client=_client(handler),
        media_fetch=None if fault == "no_fetch" else fetch,
    )
    row = {
        **MEDIA_ROW,
        "workspace_id": "ws-1",
        "payload": {**MEDIA_ROW["payload"], "media": media},
    }
    with pytest.raises(MediaAdmissionNotSent):
        await transport.for_chat("7")(row)
    assert sent == []
