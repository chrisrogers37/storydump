"""The card button's `callback_data`: minted here, parsed here (W4, phase 1 of
the 2026-09-09 tap plan, step 3).

`prompts.render_card` puts `v1:<action>:<intent-uuid>` on every button; the
ingress dispatcher reads it back. Mint and parse were two places once — the
buttons shipped for months with nothing consuming them — so both live in this
one module and a test round-trips them.

The token is bounded by Telegram's 64-byte `callback_data` limit: `v1:` (3)
plus the longest action (9, `notposted`) plus `:` (1) plus a UUID (36) is 49
bytes.

The identity link's confirmation (the `link-` lane's second step) mints its
two buttons here too, so every `callback_data` the bot puts on a button has
one mint and one parse in one module: `v1:<linkok|linkno>:<state>:<tg-user>`.
The state is `oauth_states.new_state()`'s 22 characters and a Telegram user
id is at most 20 digits, so the longest is 3 + 6 + 1 + 22 + 1 + 20 = 53 bytes.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Optional

VERSION = 1

#: The actions a card offers. `post` is the API workspace's "post now"
#: (→ `approve`), `posted` the manual workspace's "I posted it" (→
#: `mark_posted`), `skip` and `reject` their commands (F10: one tap is final;
#: no confirm token). The review card's three (2026-09-12 — the workspace
#: resolves its own `review_required` card): `itposted` (Instagram did post
#: it), `notposted` (it is not on the story — post again; the label IS the
#: member's verdict, which the resolution needs when the publish answer was
#: lost) and `giveup` (cancel), all → `resolve_review`.
ACTIONS: tuple[str, ...] = (
    "post",
    "posted",
    "skip",
    "reject",
    "itposted",
    "notposted",
    "giveup",
)


@dataclass(frozen=True)
class Tap:
    action: str
    intent_id: str
    version: int = VERSION


def token(action: str, intent_id: str) -> str:
    """`v1:<action>:<intent_id>`, refused at mint time if it would not fit."""
    data = f"v{VERSION}:{action}:{intent_id}"
    if len(data.encode()) > 64:  # Telegram's callback_data bound
        raise ValueError(f"callback token exceeds 64 bytes: {data!r}")
    return data


def parse(callback_data: Optional[str]) -> Optional[Tap]:
    """The tap a button carries, or None for anything this version does not
    mint — an older card, a foreign bot's button, a hand-typed string. Never
    raises: the dispatcher answers a None by name."""
    if not isinstance(callback_data, str):
        return None
    parts = callback_data.split(":")
    if len(parts) != 3:
        return None
    version, action, intent_id = parts
    if version != f"v{VERSION}" or action not in ACTIONS:
        return None
    try:
        uuid.UUID(intent_id)
    except ValueError:
        return None
    return Tap(action=action, intent_id=intent_id)


# ---------------------------------------------------------------------------
# The identity link's confirmation buttons
# ---------------------------------------------------------------------------

#: Confirm and Cancel on the bot's "link this Telegram account to …?" DM.
LINK_CONFIRM = "linkok"
LINK_CANCEL = "linkno"
LINK_ACTIONS: tuple[str, ...] = (LINK_CONFIRM, LINK_CANCEL)

_STATE = re.compile(r"[A-Za-z0-9_-]{1,40}")
_TELEGRAM_USER = re.compile(r"[0-9]{1,20}")


@dataclass(frozen=True)
class LinkTap:
    """A tap on the link confirmation: the action, the `link` state it
    confirms, and the Telegram user the confirmation was offered to — the one
    who opened the link. The button is minted by the bot, so the user id is
    the bot's own record of who that was, not something a tapper supplies."""

    action: str
    state: str
    telegram_user_id: str


def link_token(action: str, state: str, telegram_user_id: str) -> str:
    """`v1:<action>:<state>:<telegram_user_id>`, refused at mint time if it
    is malformed or would not fit."""
    if action not in LINK_ACTIONS:
        raise ValueError(f"unknown link action {action!r}")
    if not _STATE.fullmatch(state) or not _TELEGRAM_USER.fullmatch(
        str(telegram_user_id)
    ):
        raise ValueError("link token: malformed state or Telegram user id")
    data = f"v{VERSION}:{action}:{state}:{telegram_user_id}"
    if len(data.encode()) > 64:  # Telegram's callback_data bound
        raise ValueError("link token exceeds 64 bytes")
    return data


def parse_link(callback_data: Optional[str]) -> Optional[LinkTap]:
    """The link confirmation a button carries, or None for anything else
    (a card's button included). Never raises."""
    if not isinstance(callback_data, str):
        return None
    parts = callback_data.split(":")
    if len(parts) != 4:
        return None
    version, action, state, telegram_user_id = parts
    if version != f"v{VERSION}" or action not in LINK_ACTIONS:
        return None
    if not _STATE.fullmatch(state) or not _TELEGRAM_USER.fullmatch(telegram_user_id):
        return None
    return LinkTap(action=action, state=state, telegram_user_id=telegram_user_id)
