"""The card button's `callback_data`: minted here, parsed here (W4, phase 1 of
the 2026-09-09 tap plan, step 3).

`prompts.render_card` puts `v1:<action>:<intent-uuid>` on every button; the
ingress dispatcher reads it back. Mint and parse were two places once — the
buttons shipped for months with nothing consuming them — so both live in this
one module and a test round-trips them.

The token is bounded by Telegram's 64-byte `callback_data` limit: `v1:` (3)
plus the longest action (8, `itposted`) plus `:` (1) plus a UUID (36) is 48
bytes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional

VERSION = 1

#: The actions a card offers. `post` is the API workspace's "post now"
#: (→ `approve`), `posted` the manual workspace's "I posted it" (→
#: `mark_posted`), `skip` and `reject` their commands (F10: one tap is final;
#: no confirm token). The review card's three (2026-09-12 — the workspace
#: resolves its own `review_required` card): `retry` (post again), `itposted`
#: (Instagram did post it) and `giveup` (cancel), all → `resolve_review`.
ACTIONS: tuple[str, ...] = (
    "post",
    "posted",
    "skip",
    "reject",
    "retry",
    "itposted",
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
