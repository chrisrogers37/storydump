"""The `/start` payload router — ONE door, two disjoint purposes (`07` §2, D33/D35).

`t.me/<bot>?start=<payload>` serves two flows that must never reach each
other's lookup:

- ``inv-<token>``  resolves ONLY against ``workspace_invitations.token_hash``
- ``link-<state>`` resolves ONLY against ``oauth_states`` rows with
  ``purpose='link', provider='telegram'``

`07` §2 states the property this file exists to preserve: *"An invite token
cannot link identities and a link token cannot grant membership — enforced by
disjoint lookup tables, not convention."*

**So dispatch is on the PREFIX and a handler only ever sees its own payload
space.** A router that tried one lookup and fell back to the other would
destroy exactly that, which is why an unknown prefix is refused rather than
attempted anywhere.

## Three outcomes, and they must stay three

======================  ==============  ===============================
payload                 outcome         dispatched?
======================  ==============  ===============================
absent (bare /start)    ``greeted``     no — legitimate, not an error
known prefix            the handler's   yes
present, prefix unknown ``unrouted``    NO
======================  ==============  ===============================

Collapsing ``unrouted`` into ``greeted`` would make an unrecognised payload
render as success. It is not success: it is an unsupported client, a truncated
link, or someone probing, and all three are things an operator should see.

## THE ROUTER OWNS EVERY REFUSAL STRING, AND THAT IS A SECURITY PROPERTY

A handler that fails returns no text. One generic string is emitted here for
every non-success, across every lane.

**This is `07` §5's existence-oracle rule, not tidiness, and it will look like
a style choice to anyone who does not know that.** If each handler wrote its
own refusal, "no such invitation" and "already consumed" would eventually
differ in wording — and the difference tells an unauthenticated prober which
tokens exist. Specific, helpful refusal messages are the defect here. The
named ``outcome`` carries the detail to our logs, where it is safe.

## Handler contract (settled with lane C on #1172)

A handler is ``async def h(conn, ctx: StartContext) -> StartResult`` and runs
inside the router's transaction, so its writes commit or roll back with the
delivery's admission. Handlers do not open connections.

``ctx.chat_type`` is Telegram's RAW value, deliberately not pre-mapped to
``ck_bindings_channel``'s ``telegram_group``/``telegram_dm``: that mapping is
the bindings writer's domain rule, and a copy here would be correct the day it
was written and drift silently afterwards.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

#: What a tapper is told for EVERY non-success, whatever actually happened.
#: One string, one place — see the existence-oracle note above.
REFUSAL = "That link isn't valid, or it has already been used."

GREETED = "greeted"
UNROUTED = "unrouted"


@dataclass(frozen=True)
class StartContext:
    """What a handler receives. The prefix is already stripped."""

    payload: str
    telegram_user_id: str
    chat_id: str
    #: Telegram's own `chat.type` — 'private' | 'group' | 'supergroup'.
    chat_type: str
    display_name: Optional[str] = None


@dataclass(frozen=True)
class StartResult:
    """What a handler returns.

    ``reply`` is SUCCESS copy only and MUST be None when ``handled`` is False —
    the router supplies refusal text so no handler can leak which tokens exist.
    """

    outcome: str
    handled: bool
    reply: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.handled and self.reply is not None:
            raise ValueError(
                "a refusing handler must not supply reply text: refusal copy is"
                " the router's, so a shared door cannot become an existence oracle"
            )


Handler = Callable[[object, StartContext], Awaitable[StartResult]]


class StartRouter:
    """Prefix → handler. Registration-time checks, no runtime ambiguity."""

    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}

    def register(self, prefix: str, handler: Handler) -> None:
        if not prefix:
            raise ValueError("prefix must be non-empty")
        if prefix in self._handlers:
            raise ValueError(f"prefix {prefix!r} is already registered")
        # A prefix that prefixes another would resolve by accident of ordering.
        # Refused HERE rather than resolved at runtime, so the ambiguity cannot
        # reach a delivery.
        for existing in self._handlers:
            if existing.startswith(prefix) or prefix.startswith(existing):
                raise ValueError(
                    f"prefix {prefix!r} is ambiguous with registered {existing!r}"
                )
        self._handlers[prefix] = handler

    @staticmethod
    def payload_of(update: dict) -> Optional[str]:
        """The `/start` payload, or None when this is not a `/start` at all.

        Returns "" for a bare `/start` — distinct from None, because "not a
        start command" and "start with no payload" are different facts.
        """
        message = update.get("message")
        if not isinstance(message, dict):
            return None
        text = message.get("text")
        if not isinstance(text, str):
            return None
        stripped = text.strip()
        if stripped == "/start":
            return ""
        # Telegram appends `@botname` in groups.
        head, _, rest = stripped.partition(" ")
        if head != "/start" and not head.startswith("/start@"):
            return None
        return rest.strip()

    async def dispatch(self, conn, update: dict) -> StartResult:
        payload = self.payload_of(update)
        if payload is None:
            return StartResult(outcome="not_a_start", handled=False)
        ctx_base = _context_from(update)
        if ctx_base is None:
            # A /start we cannot attribute to a Telegram user is not routable:
            # every handler here binds something to an identity.
            logger.warning("start router: /start with no attributable sender")
            return StartResult(outcome="unattributable", handled=False)
        if payload == "":
            return StartResult(outcome=GREETED, handled=True, reply=_GREETING)

        # Longest prefix first, so registration order can never decide a match.
        for prefix in sorted(self._handlers, key=len, reverse=True):
            if payload.startswith(prefix):
                ctx = _with_payload(ctx_base, payload[len(prefix) :])
                return await self._handlers[prefix](conn, ctx)

        # NOT dispatched anywhere. D33/D35's tables are disjoint, so a payload
        # matching no prefix is not a near miss with a fallback worth trying.
        logger.warning(
            "start router: UNROUTED payload (prefix not registered); refusing"
        )
        return StartResult(outcome=UNROUTED, handled=False)


_GREETING = "Welcome to Storydump."


def _context_from(update: dict) -> Optional[StartContext]:
    message = update.get("message") or {}
    sender = message.get("from") or {}
    chat = message.get("chat") or {}
    uid, cid = sender.get("id"), chat.get("id")
    if uid is None or cid is None:
        return None
    name = sender.get("username") or sender.get("first_name")
    return StartContext(
        payload="",
        telegram_user_id=str(uid),
        chat_id=str(cid),
        chat_type=str(chat.get("type") or ""),
        display_name=name,
    )


def _with_payload(ctx: StartContext, payload: str) -> StartContext:
    return StartContext(
        payload=payload,
        telegram_user_id=ctx.telegram_user_id,
        chat_id=ctx.chat_id,
        chat_type=ctx.chat_type,
        display_name=ctx.display_name,
    )
