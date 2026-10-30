"""The Telegram outbox transport — deliver()'s injected seam, made loud.

Contract (deliver()'s words): takes the claimed outbox row, returns the
external message ref, raises to signal a lost or refused response — and
`outbox.settle` decides from what was raised what becomes of the row.

**A dead credential is a named, observable state, not a quiet one.** A
rejected token with no loud surface leaves outbound dead for an unknown period
— zero events drained, so nothing alarms:

- :meth:`TelegramTransport.probe` (`getMe`) runs at composition time; a 401
  raises :class:`TelegramAuthDead` and the worker starts WITHOUT the channel,
  parking `deliver_outbox` with the credential named in the reason — a
  recurring warning, not a one-time line.
- A mid-run 401 raises :class:`TelegramAuthDead` per send, increments
  `auth_failures` (surfaced in the worker status line), and logs loudly ONCE —
  a latch, so the log stays readable while the counter keeps counting. A 403
  is the chat's (:class:`TelegramChatGone`), never the credential's.
- The bot token never appears in any exception text or log line. The request
  URL embeds it, so every raise path out of the HTTP layer is re-raised with
  the token redacted.

Payload contract (what the prompt production emits): ``{"v": 1, "text":
str, "reply_markup": optional dict}``. A payload without text is a definitive
send error — there is nothing to send, and inventing a rendering would be a
quiet wrong message. A payload with a ``media`` block is still sent as its
text; the block rides the edit that follows (`_supersede`), which puts the
photo or video on that message by ``editMessageMedia``.

All HTTP goes through the egress floor (`api.telegram.org` is already in its
default allowlist). The outbox tick's open transaction is by design
(send-state and finalize commit together); the floor's UoW tripwire does not
cover poller sessions by construction, and nothing here flips any floor flag.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import replace as _replace
from typing import Awaitable, Callable, Optional

import httpx

from src.services.target import egress, vocabulary
from src.services.target.egress import TIMEOUT_CLASSES, EgressPolicy
from src.services.target.outbox import (
    ChannelPaced,
    ChannelRefused,
    ChannelSendError,
    CredentialDead,
    DestinationGone,
    MediaAdmissionNotSent,
)

logger = logging.getLogger("channels.telegram")

_API_BASE = vocabulary.TELEGRAM_BOT_API_BASE


#: Telegram's flood wait as it sometimes arrives: a 400 whose description
#: names the wait, with no `parameters.retry_after`.
_FLOOD = re.compile(r"too many requests: retry after (\d+)", re.IGNORECASE)

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


#: The `InputMedia` type and its multipart part name, by what Telegram will take
#: as a photo or a video; anything else (HEIC, GIF, BMP, TIFF, a codec Telegram
#: will not play) goes as a document, which still previews inline and never
#: earns a refusal for its dimensions or its container.
_PHOTO_MIMES = frozenset({"image/jpeg", "image/png"})
_VIDEO_MIMES = frozenset({"video/mp4", "video/quicktime"})
_MEDIA_KINDS = frozenset({"image", "video"})


def _media_type_for(kind: str, mime: Optional[str]) -> str:
    """The `InputMedia` type (and the multipart part name) Telegram will take
    the card's file as, by the MIME sets above."""
    m = (mime or "").lower()
    if kind == "image" and m in _PHOTO_MIMES:
        return "photo"
    if kind == "video" and m in _VIDEO_MIMES:
        return "video"
    return "document"


#: A card upload's whole budget: its `card_upload` wait, with room left to try
#: again a connection that never left.
_UPLOAD_BUDGET_S = TIMEOUT_CLASSES["card_upload"] + 30.0


class TelegramSendError(ChannelSendError):
    """The transport could not produce an external ref for this row. Telegram's
    own code, when it answered with one, rides as ``code`` (outbox failure records)."""


class TelegramChatGone(DestinationGone, TelegramSendError):
    """The chat will not take messages: the bot was kicked or blocked, the
    chat was deleted, or a group became a supergroup (Telegram names the
    successor id in `parameters.migrate_to_chat_id`). A chat-level fact —
    never the credential's."""


class TelegramPaced(ChannelPaced, TelegramSendError):
    """Telegram's flood wait — a 429 with `retry_after`, or a 400 whose
    description says "Too Many Requests: retry after N": the message did not
    go; the outbox writes the budget as a durable hold and the sender comes
    back when Telegram said to. Telegram does not say whether the flood limit
    is the bot's or the chat's; the hold is written on the GLOBAL row
    (bounded) and, for a chat-addressed call, the chat's."""


class TelegramRefused(ChannelRefused, TelegramSendError):
    """Telegram answered, and said no for good: `ok: false` with a 4xx that is
    neither a gone chat, a dead token nor a flood wait (a 429, or a 400 that
    says "Too Many Requests: retry after N") — a DEFINITIVE refusal, so a
    caller may try another shape of the same message, and one that escapes
    to the outbox fails the row (`ChannelRefused`). A transport failure, a
    flood wait or a 5xx is NOT this: the message may have landed, and only the
    outbox's ambiguity policy may decide what happens next."""


class MediaUnavailable(Exception):
    """A card's media cannot be fetched for a reason that is the FILE's and
    will not change — too large, gone, refused by the provider. The card stays
    text: its edit brings the line and the keyboard without the media, at
    WARNING."""


class MediaTransient(Exception):
    """The fetch got no usable answer (the provider is rate-limiting, erroring
    or not answering). Nothing reached Telegram, and the card's upload is made
    once: the card stays text, at WARNING, and its edit brings the line and
    the keyboard without the media. Anything else a fetch raises (a dead
    grant, a bug) is loud: ERROR, counted, and the card stays text."""


class TelegramAuthDead(CredentialDead, TelegramSendError):
    """Telegram rejected the credential itself (401; a 403 is the chat's,
    `_chat_gone`) — the loud class. For the outbox it is a `CredentialDead`,
    recorded as `credential_dead` rather than as a lost response (outbox failure records)."""


class SendReceipt(str):
    """The message ref a send returned, carrying HOW the card went out —
    ``sent_as`` is ``"text"`` (a text message: every card's create, or an edit
    after which the card stays text for good and no media is owed),
    ``"media"`` (an edit that put the card's photo or video on the message, or
    found it there) or ``"edit"`` (a line or keyboard edit) — so the outbox
    can record it and a later edit knows whether to call `editMessageCaption`
    or `editMessageText`. A `str`, so every consumer can store the ref as a
    string."""

    sent_as: Optional[str]

    def __new__(cls, ref: str, *, sent_as: Optional[str] = None):
        obj = super().__new__(cls, ref)
        obj.sent_as = sent_as
        return obj


_NOT_MODIFIED = "message is not modified"
#: Telegram's answer to an edit of a message that no longer exists.
_EDIT_GONE = "message to edit not found"
#: Telegram's answer to `editMessageText` on a photo or video message.
_NO_TEXT = "there is no text in the message to edit"
#: Telegram's answer to `editMessageCaption` on a text message.
_NO_CAPTION = "there is no caption in the message to edit"
_EMPTY_KEYBOARD = {"inline_keyboard": []}


def _with_line(body: Optional[str], line: Optional[str]) -> str:
    """A card's text with its outcome line under it: how an edit writes the
    line onto a card, and how a card sent settled goes out — its
    payload keeps the card's own text, and the line rides `outcome_text`."""
    if body and line:
        return f"{body}\n{line}"
    return str(body or line or "")


def _line_edit(
    chat_id: str, message_id, body: str, markup: dict, *, caption: bool
) -> tuple[str, dict]:
    """The call that writes a card's header and line, *body*, onto its message
    with the keyboard: the caption of a photo or video (*caption*), the text of
    a text message otherwise, each within Telegram's limit."""
    method, field, limit = (
        ("editMessageCaption", "caption", 1024)
        if caption
        else ("editMessageText", "text", 4096)
    )
    return method, {
        "chat_id": chat_id,
        "message_id": message_id,
        field: body[:limit],
        "reply_markup": markup,
    }


def _message_id(ref: str):
    """Telegram wants the integer it issued; a ref that is not one is passed
    through so the API's own refusal names it."""
    try:
        return int(ref)
    except (TypeError, ValueError):
        return ref


class TelegramTransport:
    """One bot credential; `for_chat` binds it to a binding's external ref."""

    def __init__(
        self,
        token: str,
        *,
        client: Optional[httpx.AsyncClient] = None,
        policy: Optional[EgressPolicy] = None,
        api_base: str = _API_BASE,
        media_fetch: Optional[
            Callable[[dict], Awaitable[tuple[bytes, str, Optional[str]]]]
        ] = None,
    ):
        self._token = token
        self._client = client or httpx.AsyncClient()
        self._policy = policy or EgressPolicy()
        # The tap's answer rides a 2 s single-attempt budget, derived from
        # THIS policy so a transport pointed at a loopback
        # fake (the load harness) keeps its allowlist on the answer too. The
        # route's one edit (`edit`) rides the policy itself.
        self._fast = _replace(
            self._policy, timeout_class="fast", total_budget_s=2.0, max_attempts=1
        )
        # A call that CREATES a message is attempted again only when its
        # request never left: a re-sent message is a second copy no edit can
        # name, so a lost answer propagates for the outbox's policy. An edit
        # is idempotent and keeps the floor's retries.
        self._once = _replace(self._policy, retry_after_send=False)
        self._api_base = api_base
        # `media_fetch(media) -> (bytes, filename, mime)`: how a card's media
        # block becomes bytes to upload (the worker wires the Drive adapter).
        # None = every card is its text.
        self._media_fetch = media_fetch
        #: Fetches that failed for a reason that was NOT the file's (a dead
        #: grant, a bug): each is an ERROR line and a tick here, surfaced in
        #: the worker's status line.
        self.media_fetch_failures = 0
        self.auth_failures = 0
        self._auth_dead_logged = False

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "<TOKEN>")

    def redact(self, text: str) -> str:
        """The token, wherever it appears in *text*, replaced — for callers
        that must log a failure that may embed the URL."""
        return self._redact(text)

    async def set_webhook(
        self,
        *,
        url: str,
        secret_token: str,
        allowed_updates: list[str],
        max_connections: int,
    ) -> bool:
        """`setWebhook`: the door URL, the secret Telegram must echo, the update
        kinds served and the connection cap; the pending backlog is kept."""
        await self._call(
            "setWebhook",
            {
                "url": url,
                "secret_token": secret_token,
                "allowed_updates": list(allowed_updates),
                "max_connections": int(max_connections),
                "drop_pending_updates": False,
            },
        )
        return True

    async def webhook_info(self) -> dict:
        """`getWebhookInfo`: what Telegram holds for this bot."""
        return await self._call("getWebhookInfo") or {}

    async def _call(
        self,
        method: str,
        payload: Optional[dict] = None,
        *,
        data: Optional[dict] = None,
        files: Optional[dict] = None,
        policy: Optional[EgressPolicy] = None,
    ) -> dict:
        url = f"{self._api_base}/bot{self._token}/{method}"
        body_kwargs = (
            {"data": data, "files": files} if files is not None else {"json": payload}
        )
        try:
            response = await egress.request(
                self._client, "POST", url, policy=policy or self._policy, **body_kwargs
            )
        except Exception as exc:  # noqa: BLE001 — every path may embed the URL
            raise TelegramSendError(
                f"{method}: transport failure: {self._redact(str(exc))}"
            ) from None
        try:
            body = response.json()
        except json.JSONDecodeError:
            raise TelegramSendError(
                f"{method}: non-JSON response (HTTP {response.status_code})",
                code=response.status_code,
            ) from None
        if body.get("ok") is True:
            return body.get("result") or {}
        code = body.get("error_code", response.status_code)
        description = self._redact(str(body.get("description", "")))
        parameters = body.get("parameters") or {}
        gone = _chat_gone(code, description)
        if gone is not None:
            migrate_to = parameters.get("migrate_to_chat_id")
            raise TelegramChatGone(
                f"{method}: {code} {description}",
                migrate_to=None if migrate_to is None else str(migrate_to),
                code=code,
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
            raise TelegramAuthDead(f"{method}: {code} {description}", code=code)
        flood = _FLOOD.search(description)
        if code == 429 or flood is not None:
            # Telegram's flood wait: a 429 carrying `retry_after`, or the same
            # wait re-coded as a 400 whose description names it. Either way
            # the message did not go, and the sender comes back when told.
            retry_after = parameters.get("retry_after")
            if retry_after is None and flood is not None:
                retry_after = flood.group(1)
            try:
                retry_after_s = max(1.0, float(retry_after))
            except (TypeError, ValueError):
                retry_after_s = 5.0
            has_chat = (isinstance(payload, dict) and "chat_id" in payload) or (
                isinstance(data, dict) and "chat_id" in data
            )
            raise TelegramPaced(
                f"{method}: {code} retry_after={retry_after_s:g}s {description}",
                retry_after_s=retry_after_s,
                scope="chat" if has_chat else "global",
                code=code,
            )
        if code in (400, 413):
            # Bad Request / Payload Too Large: the message as shaped will
            # never be accepted. A flood wait (above), a 5xx and a lost answer
            # are NOT this — the message may have landed, or may still go.
            raise TelegramRefused(f"{method}: {code} {description}", code=code)
        raise TelegramSendError(f"{method}: {code} {description}", code=code)

    async def answer_callback(
        self, callback_query_id: str, text: str, show_alert: bool = False
    ) -> bool:
        """`answerCallbackQuery` — the tap's toast (or alert). Best effort by
        contract: it is the client's spinner, worthless late and harmless lost,
        so a failure is logged and False, never raised past the route."""
        try:
            await self._call(
                "answerCallbackQuery",
                {
                    "callback_query_id": str(callback_query_id),
                    "text": text,
                    "show_alert": bool(show_alert),
                },
                policy=self._fast,
            )
        except Exception as exc:  # noqa: BLE001 — best effort, by contract
            # A query Telegram already considers answered (a redelivery, a
            # slow tap) is the expected shape, not a fault: INFO.
            level = (
                logger.info
                if isinstance(exc, TelegramRefused)
                and ("too old" in str(exc) or "QUERY_ID_INVALID" in str(exc))
                else logger.warning
            )
            level(
                "answerCallbackQuery %s not delivered: %s",
                callback_query_id,
                self._redact(str(exc)),
            )
            return False
        return True

    async def _edit(
        self, method: str, payload: dict, *, policy: Optional[EgressPolicy] = None
    ) -> None:
        """One editMessage* call. Telegram's "message is not modified" is a
        400 that means the edit already stands — success here."""
        try:
            await self._call(method, payload, policy=policy)
        except TelegramRefused as exc:
            if _NOT_MODIFIED in str(exc):
                return
            raise

    async def _supersede(self, external_ref: str, row: dict) -> SendReceipt:
        """A `prompt_supersede` row edits the card it names. When the card's
        photo or video is not yet on its message — the sender gave the row the
        card's `media` block at claim, and `sent_as` is not `media` — ONE
        `editMessageMedia` brings it, with the card's header and its line as
        the caption and the keyboard the sender gave the row. The edit names
        the message, so whatever a lost answer leaves there — the photo or
        the text — the next edit lands on that same message, never as a second
        card, and the upload is made once: a lost answer is settled ambiguous
        and the outbox gives the card's media up, so the resent edit brings
        the line and the keyboard alone. Otherwise one call writes the header
        plus the outcome line (a caption for a media card, text otherwise)
        with the keyboard, or the keyboard alone for a row without an outcome.
        The keyboard is the story's (Configure alone on a settled card), or an
        empty one for a rejected story, a removed item or an invitation. If
        the media is refused (a file Telegram will not take) or cannot be
        fetched, the card stays text: the line and the keyboard land without
        it, and the receipt says `text`, so the sender records that no media
        is owed. When the message is not the type the record says (a text edit
        refused because the message is a photo or video, or a caption edit
        refused because it is text), the other edit is tried once and the
        receipt says how the message stands, so the sender corrects the card's
        record. If a combined edit is refused, the keyboard still lands by the
        type-agnostic `editMessageReplyMarkup`. Only a definitive refusal
        takes a fallback, and not one saying the message is gone: a flood
        wait escapes and the same edit, media included, goes again after the
        hold; a 5xx or a transport failure escapes as ambiguous. A refusal of
        the fallback escapes too, and fails the row."""
        payload = row.get("payload") or {}
        ref = str(payload["supersedes_ref"])
        message_id = _message_id(ref)
        outcome = payload.get("outcome_text")
        # The story's keyboard, which the sender gave the edit at claim;
        # an invitation's edit leaves none.
        markup = payload.get("reply_markup") or _EMPTY_KEYBOARD
        sent_as = payload.get("sent_as")
        media = payload.get("media")
        body = _with_line(payload.get("header"), outcome)
        done = SendReceipt(ref, sent_as="edit")
        if media and sent_as != "media":
            fetched = None
            if self._media_fetch is not None and media.get("kind") in _MEDIA_KINDS:
                fetched = await self._card_media(row, media)
            if fetched is not None:
                content, filename, mime = fetched
                try:
                    await self.edit_media(
                        external_ref,
                        ref,
                        kind=str(media["kind"]),
                        content=content,
                        filename=filename,
                        mime=mime,
                        caption=body,
                        reply_markup=markup,
                    )
                    return SendReceipt(ref, sent_as="media")
                except TelegramRefused as exc:
                    if _EDIT_GONE in str(exc):
                        raise
                    logger.warning(
                        "outbox row %s: media refused on the edit (%s) — the card"
                        " stays text; its line and keyboard land without it",
                        row.get("id"),
                        self._redact(str(exc)),
                    )
            # The card stays text, and no later edit brings the media.
            sent_as, done = "text", SendReceipt(ref, sent_as="text")
        if outcome:
            caption = sent_as == "media"
            method, call = _line_edit(
                external_ref, message_id, body, markup, caption=caption
            )
            try:
                await self._edit(method, call)
                return done
            except TelegramRefused as exc:
                if _EDIT_GONE in str(exc):
                    # The message is gone (deleted from the chat): no edit can
                    # land on it, so the keyboard-only call is not made.
                    raise
                if _NO_TEXT in str(exc) or _NO_CAPTION in str(exc):
                    # Not the type the record says: the other edit, once.
                    caption = _NO_TEXT in str(exc)
                    method, call = _line_edit(
                        external_ref, message_id, body, markup, caption=caption
                    )
                    try:
                        await self._edit(method, call)
                        return SendReceipt(ref, sent_as="media" if caption else "text")
                    except TelegramRefused as again:
                        if _EDIT_GONE in str(again):
                            raise
                        exc = again
                logger.warning(
                    "%s refused for a supersede (%s); stripping the keyboard alone",
                    method,
                    self._redact(str(exc)),
                )
        await self._edit(
            "editMessageReplyMarkup",
            {
                "chat_id": external_ref,
                "message_id": message_id,
                "reply_markup": markup,
            },
        )
        return done

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
         are both this call."""
        message: dict = {"chat_id": chat_id, "text": text}
        if reply_markup:
            message["reply_markup"] = reply_markup
        result = await self._call("sendMessage", message, policy=self._once)
        message_id = (result or {}).get("message_id")
        if message_id is None:
            raise TelegramSendError("sendMessage: ok response without a message_id")
        return str(message_id)

    async def edit(
        self,
        chat_id: str,
        message_id: str,
        *,
        text: Optional[str] = None,
        markup: Optional[dict] = None,
    ) -> None:
        """The route's one edit of a tapped message. With *text*,
        one `editMessageText` that also sets the keyboard (none unless
        *markup* says): the identity link's prompt, rewritten to its outcome,
        or an admitted waitlist ping, its Admit button gone.
        With *markup* alone, one `editMessageReplyMarkup`: a card's Configure
        menu, or Back to the card. Telegram's "message is not modified" is
        success."""
        body = {
            "chat_id": chat_id,
            "message_id": _message_id(message_id),
            "reply_markup": markup if markup is not None else _EMPTY_KEYBOARD,
        }
        if text is not None:
            await self._edit("editMessageText", {**body, "text": text[:4096]})
        else:
            await self._edit("editMessageReplyMarkup", body)

    async def edit_media(
        self,
        chat_id: str,
        message_id: str,
        *,
        kind: str,
        content: bytes,
        filename: str,
        mime: Optional[str],
        caption: str,
        reply_markup: Optional[dict],
    ) -> dict:
        """One `editMessageMedia`: the card's photo or video uploaded onto the
        message *message_id* names — a text message becomes the media — with
        *caption* and the keyboard, in one request. The upload runs under the
        `card_upload` wait and is never sent twice inside the call (`_once`).
        A lost answer is the outbox's: it settles the edit ambiguous, gives the
        card's media up and resends the edit without it, to the same message
        id, so the upload is made once per card and a second one here would
        hold this chat's sender longer for nothing. The outbox commits its
        pacing and its claim before this call, so a slow upload holds back only
        this chat's next message, never the global rate row. "Message is not
        modified" is success. Returns Telegram's edited message."""
        media_type = _media_type_for(kind, mime)
        data = {
            "chat_id": chat_id,
            "message_id": _message_id(message_id),
            "media": json.dumps(
                {
                    "type": media_type,
                    "media": f"attach://{media_type}",
                    "caption": caption[:1024],
                }
            ),
            "reply_markup": json.dumps(
                reply_markup if reply_markup is not None else _EMPTY_KEYBOARD
            ),
        }
        files = {media_type: (filename, content, mime or "application/octet-stream")}
        policy = _replace(
            self._once,
            timeout_class="card_upload",
            total_budget_s=max(self._policy.total_budget_s, _UPLOAD_BUDGET_S),
        )
        try:
            return await self._call(
                "editMessageMedia", data=data, files=files, policy=policy
            )
        except TelegramRefused as exc:
            if _NOT_MODIFIED in str(exc):
                return {}
            raise

    async def _card_media(
        self, row: dict, media: dict
    ) -> Optional[tuple[bytes, str, Optional[str]]]:
        """The bytes a card's edit carries, or None when the card stays text.
        The media block is written in-process, but the grant it selects must
        agree with the outbox row's workspace: a row whose block names another
        workspace — or a row that cannot vouch for one at all — is refused,
        loudly, before any fetch. The guard stays HERE, on the one path that
        fetches: a guard that lives elsewhere is a guard a reader misses."""
        row_ws = row.get("workspace_id")
        if row_ws is None or str(media.get("workspace_id")) != str(row_ws):
            self.media_fetch_failures += 1
            logger.error(
                "outbox row %s: media block names workspace %s but the row"
                " belongs to %s — fetch refused; the card stays text",
                row.get("id"),
                media.get("workspace_id"),
                row_ws,
            )
            return None
        return await self._fetch_media(row, media)

    async def _fetch_media(
        self, row: dict, media: dict
    ) -> Optional[tuple[bytes, str, Optional[str]]]:
        """The media block's bytes for the edit that carries them, or None
        when the card stays text.

        THE FOUR ARMS ARE THE DEGRADATION BEHAVIOUR, each with the log line
        that names its cause: an unavailable file is the file's own fault; a
        transient blip, and a past-posts story whose bytes are not admitted
        now, cost the picture, not the card, because the card's upload is made
        once (a raise here would be settled as a lost answer, and the resent
        edit would bring no media anyway) — all three at WARNING; anything else
        is a bug or a dead grant and is counted as such (ERROR), as is a
        past-posts preparation that failed. Nothing leaves this method by
        raising: the edit goes on to bring the line and the keyboard alone.
        """
        try:
            return await self._media_fetch(media)
        except MediaAdmissionNotSent as exc:
            if exc.preparation_failed:
                self.media_fetch_failures += 1
                logger.error(
                    "past-posts media preparation failed before channel dispatch"
                )
            logger.warning(
                "outbox row %s: media not admitted (%s) — the card stays text",
                row.get("id"),
                exc.reason,
            )
        except MediaUnavailable as exc:
            logger.warning(
                "outbox row %s: media unavailable (%s) — the card stays text",
                row.get("id"),
                self._redact(str(exc)),
            )
        except MediaTransient as exc:
            logger.warning(
                "outbox row %s: media fetch got no answer (%s) — the card stays text",
                row.get("id"),
                self._redact(str(exc)),
            )
        except Exception as exc:  # noqa: BLE001 — degraded, but never quietly
            self.media_fetch_failures += 1
            logger.error(
                "outbox row %s: media fetch FAILED (%s: %s) — the card stays"
                " text; this is not the file's fault",
                row.get("id"),
                type(exc).__name__,
                self._redact(str(exc)),
            )
        return None

    def for_chat(self, external_ref: str):
        """The per-binding sender deliver() takes: row in, message ref out.

        Every card is sent as its `text`, with no keyboard; a card with a
        `media` block gets its photo or video, and its keyboard, by the edit
        the sender queues once this send is confirmed (`_supersede`), so the
        upload names the message it lands on and a late answer can never leave
        a second card. A past-posts card whose block cannot be trusted sends
        nothing. A gone chat and a dead token propagate; a lost answer
        propagates as ambiguous, as every send does.
        """

        async def send(row: dict) -> SendReceipt:
            if row.get("kind") == "prompt_supersede":
                return await self._supersede(external_ref, row)
            payload = row.get("payload") or {}
            text_body = payload.get("text")
            media = payload.get("media")
            if (
                media
                and media.get("intent_id") is not None
                and (
                    self._media_fetch is None
                    or media.get("kind") not in _MEDIA_KINDS
                    or row.get("workspace_id") is None
                    or str(media.get("workspace_id")) != str(row["workspace_id"])
                    or str(media["intent_id"]) != str(row.get("intent_id"))
                )
            ):
                # A past-posts card's bytes are the server-selected intent's:
                # a block that names no fetch, another workspace or another
                # story sends nothing, not even the text its edit would arm.
                self.media_fetch_failures += 1
                raise MediaAdmissionNotSent(
                    "past_posts_revalidation_required", preparation_failed=True
                )
            if not text_body:
                raise TelegramSendError(
                    f"outbox row {row.get('id')}: payload carries no text —"
                    " nothing to send"
                )
            return SendReceipt(
                await self.send_text(
                    external_ref,
                    _with_line(text_body, payload.get("outcome_text")),
                    reply_markup=payload.get("reply_markup"),
                ),
                sent_as="text",
            )

        return send

    async def aclose(self) -> None:
        await self._client.aclose()


#: A loopback Telegram for the load harness (`tests/scripts/load`): the API
#: and the worker are real subprocesses, Telegram is a fake on this machine.
API_BASE_VAR = "TARGET_TELEGRAM_API_BASE"
#: Loopback ADDRESSES only — `localhost` is a name /etc/hosts may point elsewhere.
_LOOPBACK = ("127.0.0.1", "::1")


class ApiBaseRefused(ValueError):
    """`TARGET_TELEGRAM_API_BASE` names a host this process may not speak to."""


def transport_from_env(token: str, env, **kwargs) -> "TelegramTransport":
    """The bot transport a process speaks with. Without `TARGET_TELEGRAM_API_BASE`
    it is the real Telegram under the default floor. With it — ONLY a loopback
    host, and NEVER in Railway's production environment — the transport points
    at that base and its floor admits the host (the private-address block is
    lifted for it; every other guard stays), so the load harness can stand a
    fake Telegram beside the real processes. The same door serves the API and
    the worker so the two cannot disagree about which Telegram they speak to."""
    base = (env.get(API_BASE_VAR) or "").strip()
    if not base:
        return TelegramTransport(token, **kwargs)
    if vocabulary.is_production(env):
        raise ApiBaseRefused(f"{API_BASE_VAR} is not honoured in production")
    host = httpx.URL(base).host
    if host not in _LOOPBACK:
        raise ApiBaseRefused(f"{API_BASE_VAR} must name a loopback host, not {host!r}")
    # The fake is the ONLY host this transport may speak to: a transport
    # pointed at a loopback fake has no business with the real providers.
    policy = EgressPolicy(
        allowed_hosts=frozenset({host}), enforce_private_address_block=False
    )
    return TelegramTransport(token, api_base=base.rstrip("/"), policy=policy, **kwargs)
