"""The Telegram half of an invitation's delivery.

An invitation has two delivery arms and `identity_and_tenancy.py` makes them one object:
`workspace_invitations.delivery_channel` is closed at `email | telegram`, and
each carries its OWN acceptance value — `email` for a verified Google
claim, `invited_tg_user_id` for the provider's immutable numeric id. The email
arm is sent after the invitation commits (`invitations.InvitationEmail`); this
arm writes a `channel_outbox` row per push binding, which
`work_loop.ensure_sender_jobs` then mints a `deliver_outbox` job for. Two
mechanisms, one credential.

## Why a card is not a broadcast of an email invitation

An email invitation's token is minted for one inbox. Posting it into a group
chat would be a credential broadcast — and it would **degrade quietly rather
than refuse**, which is worse: `identity_and_tenancy.py` makes `email` the acceptance value, so
a Telegram tapper fails the identity match, takes the recorded-skip path, and
lands `member` plus an elevation-pending notification. It would look like it
worked. So this producer announces `delivery_channel = 'telegram'` invitations
only, and refuses anything else by name.

## What an open card can and cannot do

A card in a group is readable by everyone in it, and possession of the token is
the whole authorization (`fn_invitation_accept` resolves by `token_hash`
alone). That is the design rather than a hole: a hint-only Telegram invitation
has no identity proof to match, so the door grants `member` and downgrades an
admin invitation with an elevation-pending notification. **An open card cannot
elevate anyone**, which is what makes the shape safe.

**Invitations are per person, by email.** A member can approve stories, and
approving one posts to Instagram, so an open card is an open link, and
`invite_member` refuses the Telegram channel.

It does mean the payload must carry nothing person-identifying. The invitee's
email address is the OTHER arm's delivery address and is never put in a card.

## Nothing calls this module

`invite_member` refuses the Telegram channel, and no code makes the
separate call that announces a card. So no Telegram invitation is
minted through the command port, and no card is produced: **the card
path has no caller.**

The minting side (`invite_member`) states the same fact, so neither half of the
seam claims a completeness the pair does not have — a surface that is built,
reachable in principle, and connected to nothing.

Its caller belongs in the transaction of the invitation write,
`invitations.create`.

## The empty case is a quiet beat, not `UNDELIVERABLE`

`outbox.UNDELIVERABLE` exists because "reached no delivery surface" was being
recorded as a clean run, and its rule is that each producer decides what an
empty binding set means BEFORE calling. For an invitation the answer is a quiet
beat: the run is a **whole-invitation** claim and there are two arms, so a
workspace with no bindings and a delivered email has not failed at anything.

**The bound on that, stated because it is the thing the rule could get
wrong:** it is right for zero bindings and would be wrong as a blanket rule. An
invitation whose workspace HAS bindings and which silently enqueued nothing
would pass as created while quietly announcing nothing.

**That second case is NOT reachable here, and the bare count is honest only
because of that.** The loop enqueues one card per binding with no per-item
guard, so a failure raises and rolls back with the invitation rather than
returning a short count — no input to this code writes fewer cards than there
are bindings. So `0` means exactly one thing: there was nothing to write
to. A single integer could not carry both meanings at once, which is why, if
per-item failure handling is ever added, the return type has to change in the
same commit. That is the point to revisit this — with a real divergent case to
test against rather than an imagined one.
"""

from __future__ import annotations

from typing import Any, Mapping

from src.exceptions.base import StorydumpError
from src.services.target import outbox, prompts

#: The channel this producer serves. `identity_and_tenancy.py`'s `ck_invite_channel` closes the
#: set; this names the half we announce.
TELEGRAM = "telegram"


class CardRefused(StorydumpError):
    """An invitation this producer will not announce."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        super().__init__(f"card refused: {reason}" + (f" — {detail}" if detail else ""))


def render_card(invitation: Mapping[str, Any], token: str) -> dict:
    """The channel-NEUTRAL payload a binding's sender renders.

    Deliberately minimal, and the omission is the point: this lands in a group
    chat, so it carries the token, the role ceiling and an optional display
    hint — never the invitee's email, which is the other arm's address.
    """
    payload: dict[str, Any] = {
        "v": 1,
        "invitation_id": str(invitation["id"]),
        "token": token,
        "role": str(invitation.get("role") or "member"),
    }
    hint = invitation.get("invited_channel_hint")
    if hint:
        payload["invited_hint"] = str(hint)
    return payload


async def announce(
    session,
    *,
    workspace_id: str,
    invitation: Mapping[str, Any],
    token: str,
) -> int:
    """Enqueue one invitation card per push binding. Returns how many.

    `0` means there were no push bindings — nothing else in this implementation
    can produce a zero, and the module note says why that is load-bearing.

    Runs in the caller's transaction: an invitation and the card announcing it
    must commit together or not at all (the same-tx rule, and
    `outbox.enqueue`'s own).

    **Bindings are resolved through `prompts.push_bindings`, not a local
    query.** That function states it is the one owner of "where can we say
    this", and it is: six production callers route on it. A second spelling is
    how two surfaces drift — the more so here, because it filters
    `channel LIKE 'telegram%'` and a general "active bindings" predicate would
    silently widen to a non-Telegram channel the day one is added.
    """
    channel = invitation.get("delivery_channel")
    if channel != TELEGRAM:
        raise CardRefused(
            "not_a_telegram_invitation",
            f"delivery_channel is {channel!r}; announcing an email invitation"
            " in a chat would broadcast a token minted for one inbox",
        )
    if not isinstance(token, str) or not token.strip():
        raise CardRefused("token_required")

    bindings = await prompts.push_bindings(session, str(workspace_id))
    if not bindings:
        # A quiet beat, not UNDELIVERABLE — see the module note.
        return 0

    payload = render_card(invitation, token.strip())
    for binding_id in bindings:
        await outbox.enqueue(
            session,
            workspace_id=str(workspace_id),
            binding_id=str(binding_id),
            kind="invitation",
            payload=payload,
        )
    return len(bindings)
