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
quiet wrong message.

All HTTP goes through the egress floor (`api.telegram.org` is already in its
default allowlist). The outbox tick's open transaction is by design
(send-state and finalize commit together); the floor's UoW tripwire does not
cover poller sessions by construction, and nothing here flips any floor flag.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace as _replace
from typing import Awaitable, Callable, Optional

import httpx

from src.services.target import egress, vocabulary
from src.services.target.egress import EgressPolicy
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


#: The upload method and its multipart part name, by what Telegram will take
#: as a photo or a video; anything else (HEIC, GIF, BMP, TIFF, a codec Telegram
#: will not play) goes as a document, which still previews inline and never
#: earns a refusal for its dimensions or its container.
_PHOTO_MIMES = frozenset({"image/jpeg", "image/png"})
_VIDEO_MIMES = frozenset({"video/mp4", "video/quicktime"})
_MEDIA_KINDS = frozenset({"image", "video"})


def _method_for(kind: str, mime: Optional[str]) -> tuple[str, str]:
    m = (mime or "").lower()
    if kind == "image" and m in _PHOTO_MIMES:
        return "sendPhoto", "photo"
    if kind == "video" and m in _VIDEO_MIMES:
        return "sendVideo", "video"
    return "sendDocument", "document"


#: A 50 MB video over a slow link needs more than the 30 s default budget.
_UPLOAD_BUDGET_S = 120.0


class TelegramSendError(ChannelSendError):
    """The transport could not produce an external ref for this row. Telegram's
    own code, when it answered with one, rides as ``code`` (outbox failure records)."""


class TelegramChatGone(DestinationGone, TelegramSendError):
    """The chat will not take messages: the bot was kicked or blocked, the
    chat was deleted, or a group became a supergroup (Telegram names the
    successor id in `parameters.migrate_to_chat_id`). A chat-level fact —
    never the credential's."""


class TelegramPaced(ChannelPaced, TelegramSendError):
    """429 Too Many Requests with Telegram's `retry_after`:
    the message did not go; the outbox writes the budget as a durable hold
    and the sender comes back when Telegram said to. Telegram does not say
    whether the flood limit is the bot's or the chat's; the hold is written
    on the GLOBAL row (bounded) and, for a chat-addressed call, the chat's."""


class TelegramRefused(ChannelRefused, TelegramSendError):
    """Telegram answered, and said no for good: `ok: false` with a 4xx that is
    neither a gone chat, a dead token nor 429 — a DEFINITIVE refusal, so a
    caller may try another shape of the same message, and one that escapes
    to the outbox fails the row (`ChannelRefused`). A transport failure, a
    429 or a 5xx is NOT this: the message may have landed, and only the
    outbox's ambiguity policy may decide what happens next."""


class MediaUnavailable(Exception):
    """A card's media cannot be fetched for a reason that is the FILE's and
    will not change — too large, gone, refused by the provider. The text card
    goes, at WARNING."""


class MediaTransient(Exception):
    """The fetch got no usable answer (the provider is rate-limiting, erroring
    or not answering). Nothing reached Telegram, so the send propagates as
    ambiguous and the outbox's policy resends later — the photo card is not
    given up for a blip. Anything else a fetch raises (a dead grant, a bug)
    is loud: ERROR, counted, and the text card goes."""


class TelegramAuthDead(CredentialDead, TelegramSendError):
    """Telegram rejected the credential itself (401; a 403 is the chat's,
    `_chat_gone`) — the loud class. For the outbox it is a `CredentialDead`,
    recorded as `credential_dead` rather than as a lost response (outbox failure records)."""


class SendReceipt(str):
    """The message ref a send returned, carrying HOW the card went out —
    ``sent_as`` is ``"media"`` (a photo or video with a caption), ``"text"``
    or ``"edit"`` — so the outbox can record it and a later edit knows whether
    to call `editMessageCaption` or `editMessageText`. A `str`, so every
    consumer can store the ref as a string."""

    sent_as: Optional[str]

    def __new__(cls, ref: str, *, sent_as: Optional[str] = None):
        obj = super().__new__(cls, ref)
        obj.sent_as = sent_as
        return obj


_NOT_MODIFIED = "message is not modified"
_EMPTY_KEYBOARD = {"inline_keyboard": []}


def _with_line(body: Optional[str], line: Optional[str]) -> str:
    """A card's text with its outcome line under it: how an edit writes the
    line onto a card, and how a card sent settled goes out — its
    payload keeps the card's own text, and the line rides `outcome_text`."""
    if body and line:
        return f"{body}\n{line}"
    return str(body or line or "")


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
        gone = _chat_gone(code, description)
        if gone is not None:
            migrate_to = (body.get("parameters") or {}).get("migrate_to_chat_id")
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
        if code in (400, 413):
            # Bad Request / Payload Too Large: the message as shaped will
            # never be accepted. 429 and 5xx are NOT this — the card may have
            # landed, and only the outbox's policy may decide.
            raise TelegramRefused(f"{method}: {code} {description}", code=code)
        if code == 429:
            retry_after = (body.get("parameters") or {}).get("retry_after")
            try:
                retry_after_s = max(1.0, float(retry_after))
            except (TypeError, ValueError):
                retry_after_s = 5.0
            has_chat = (isinstance(payload, dict) and "chat_id" in payload) or (
                isinstance(data, dict) and "chat_id" in data
            )
            raise TelegramPaced(
                f"{method}: 429 retry_after={retry_after_s:g}s {description}",
                retry_after_s=retry_after_s,
                scope="chat" if has_chat else "global",
                code=code,
            )
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
        """A `prompt_supersede` row edits the card it names in ONE call: the
        original header plus the outcome line — a caption for a media card,
        text otherwise — with the keyboard the sender gave the row in the same
        request: the story's ( — ⚙️ Configure alone on a settled
        card), or an empty one for a rejected story, a removed item or an
        invitation. So the decision buttons go as the line lands (one Telegram
        message per tap per binding; the bot's 30/s is the fleet's ceiling).
        A row without an outcome edits the keyboard alone. If the
        combined edit is refused (a caption Telegram will not take), the
        keyboard still lands by the type-agnostic `editMessageReplyMarkup` —
        the call that must land. Only a definitive refusal takes the fallback:
        a 429, a 5xx or a transport failure escapes, and the
        outbox settles it (paced, or ambiguous → resent under the prompt cap →
        failed). A refusal of the fallback escapes too, and fails the row."""
        payload = row.get("payload") or {}
        ref = str(payload["supersedes_ref"])
        message_id = _message_id(ref)
        outcome = payload.get("outcome_text")
        # The story's keyboard, which the sender gave the edit at claim;
        # an invitation's edit leaves none.
        markup = payload.get("reply_markup") or _EMPTY_KEYBOARD
        if outcome:
            body = _with_line(payload.get("header"), outcome)
            if payload.get("sent_as") == "media":
                method, field, limit = "editMessageCaption", "caption", 1024
            else:
                method, field, limit = "editMessageText", "text", 4096
            try:
                await self._edit(
                    method,
                    {
                        "chat_id": external_ref,
                        "message_id": message_id,
                        field: body[:limit],
                        "reply_markup": markup,
                    },
                )
                return SendReceipt(ref, sent_as="edit")
            except TelegramRefused as exc:
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
        return SendReceipt(ref, sent_as="edit")

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

    async def send_media(
        self,
        chat_id: str,
        *,
        kind: str,
        content: bytes,
        filename: str,
        mime: Optional[str],
        caption: str,
        reply_markup: Optional[dict] = None,
    ) -> str:
        """One `sendPhoto` / `sendVideo` / `sendDocument` upload — the card:
        the media, the caption, the keyboard.
        Runs under the upload timeout class with a budget wide enough for the
        bytes. Note the outbox paces inside the sender's transaction, so the
        global rate row stays locked for the upload's duration."""
        method, part = _method_for(kind, mime)
        data: dict = {"chat_id": chat_id, "caption": caption[:1024]}
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)
        files = {part: (filename, content, mime or "application/octet-stream")}
        # Never sent twice, as every message-creating call (`_once`).
        policy = _replace(
            self._once,
            timeout_class="upload",
            total_budget_s=max(self._policy.total_budget_s, _UPLOAD_BUDGET_S),
        )
        result = await self._call(method, data=data, files=files, policy=policy)
        message_id = (result or {}).get("message_id")
        if message_id is None:
            raise TelegramSendError(f"{method}: ok response without a message_id")
        return str(message_id)

    async def _fetch_media(
        self, row: dict, media: dict
    ) -> Optional[tuple[bytes, str, Optional[str]]]:
        """The media block's bytes, or None when the card must go as text.

        THE THREE ARMS ARE THE DEGRADATION BEHAVIOUR and none is
        interchangeable with another: an unavailable file is the file's own
        fault and degrades quietly, a transient blip is ambiguous and is
        re-raised so the outbox resends WITH the photo — once — and anything
        else is a bug or a dead grant and is counted as such. `MediaTransient`
        is the only arm that can leave this method by raising.
        """
        try:
            return await self._media_fetch(media)
        except MediaAdmissionNotSent as exc:
            if exc.preparation_failed:
                self.media_fetch_failures += 1
                logger.error(
                    "past-posts media preparation failed before channel dispatch"
                )
            # The past-posts source has not authorized these bytes; no text fallback or
            # channel request has occurred, regardless of earlier attempts.
            raise
        except MediaUnavailable as exc:
            logger.warning(
                "outbox row %s: media unavailable (%s) — sending the text card",
                row.get("id"),
                self._redact(str(exc)),
            )
        except MediaTransient as exc:
            # Nothing reached Telegram: ambiguous by the outbox's
            # book, resent later with the photo intact — ONCE.
            # An ambiguous approval_prompt returns to pending with
            # no attempts cap, so a provider outage would otherwise
            # loop the card forever; the second attempt sends the
            # text card.
            if int(row.get("attempts") or 1) < 2:
                raise TelegramSendError(
                    f"media fetch got no answer: {self._redact(str(exc))}"
                ) from exc
            logger.warning(
                "outbox row %s: media fetch got no answer again (%s) —"
                " sending the text card",
                row.get("id"),
                self._redact(str(exc)),
            )
        except Exception as exc:  # noqa: BLE001 — degraded, but never quietly
            self.media_fetch_failures += 1
            logger.error(
                "outbox row %s: media fetch FAILED (%s: %s) — sending the"
                " text card; this is not the file's fault",
                row.get("id"),
                type(exc).__name__,
                self._redact(str(exc)),
            )
        return None

    async def _send_media_card(
        self,
        external_ref: str,
        row: dict,
        payload: dict,
        media: dict,
        fetched: tuple[bytes, str, Optional[str]],
    ) -> Optional[SendReceipt]:
        """The upload and its receipt, or None when Telegram DEFINITIVELY
        refused it and the text card should go instead.

        Only `TelegramRefused` is caught, and that is the whole point: a
        transport failure may have landed the card, so it propagates for the
        outbox's ambiguity policy. A gone chat and a dead token propagate too,
        and `outbox.settle` decides what each means for the row.
        """
        content, filename, mime = fetched
        try:
            return SendReceipt(
                await self.send_media(
                    external_ref,
                    kind=str(media["kind"]),
                    content=content,
                    filename=filename,
                    mime=mime,
                    caption=_with_line(
                        payload.get("caption") or payload.get("text"),
                        payload.get("outcome_text"),
                    ),
                    reply_markup=payload.get("reply_markup"),
                ),
                sent_as="media",
            )
        except TelegramRefused as exc:
            logger.warning(
                "outbox row %s: upload refused (%s) — sending the text card",
                row.get("id"),
                self._redact(str(exc)),
            )
        return None

    def for_chat(self, external_ref: str):
        """The per-binding sender deliver() takes: row in, message ref out.

        A payload with a `media` block is sent as the photo or video with its
        `caption` when the bytes can be fetched and Telegram accepts them;
        otherwise the `text` card goes. Only a DEFINITIVE refusal of the
        upload falls back — a transport failure may have landed the card, and
        propagates for the outbox's ambiguity policy, as every send does; a
        gone chat and a dead token propagate too.
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
                self.media_fetch_failures += 1
                raise MediaAdmissionNotSent(
                    "past_posts_revalidation_required", preparation_failed=True
                )
            if (
                media
                and self._media_fetch is not None
                and media.get("kind") in _MEDIA_KINDS
            ):
                fetched = None
                row_ws = row.get("workspace_id")
                if row_ws is None or str(media.get("workspace_id")) != str(row_ws):
                    # The media block is written in-process, but the grant it
                    # selects must agree with the outbox row's workspace: a row
                    # whose block names another workspace — or a row that
                    # cannot vouch for one at all — is refused, loudly.
                    #
                    # The guard stays HERE, in `send`: a guard that lives
                    # elsewhere is a guard a reader misses.
                    self.media_fetch_failures += 1
                    logger.error(
                        "outbox row %s: media block names workspace %s but the row"
                        " belongs to %s — fetch refused; sending the text card",
                        row.get("id"),
                        media.get("workspace_id"),
                        row_ws,
                    )
                else:
                    fetched = await self._fetch_media(row, media)
                if fetched is not None:
                    receipt = await self._send_media_card(
                        external_ref, row, payload, media, fetched
                    )
                    if receipt is not None:
                        return receipt
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
