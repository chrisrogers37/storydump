"""The `06` Telegram join path (#1242): a message in a BOUND group from a
Telegram user whose identity is linked makes that user a workspace member.

Message-driven, because the Bot API has no "list the members" for a group:
the door sees people as they speak or are added — which, under Telegram's
default privacy mode, means commands, replies, mentions and service messages
only. The runbook names the precondition (privacy mode off, or the bot a
group admin). The write is the `fn_group_member_seen`
door (`07` §14) — `svc_ingress` holds no grant on `workspace_members` and
needs none; the door sets the actor GUCs and never downgrades. Leaving a
group removes nothing (`06`): an admin removes membership explicitly.

Every outcome is named and none is answered in the chat: this is not a
`/start`, and a bot that greeted every first message would be noise.
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from src.services.target import bindings, identity, readers
from src.services.target.start_router import StartResult

logger = logging.getLogger(__name__)

PROVIDER = identity.PROVIDER_TELEGRAM
GROUP_CHAT_TYPES = ("group", "supergroup")


def group_members_of(update: dict) -> list[tuple[str, str, str]]:
    """`[(chat_type, external_ref, telegram_user_id), …]` for the people a
    group message shows the bot: the sender, and — on a `new_chat_members`
    service message — the people added (the sender of that message is the
    adder). Empty for a DM, a channel post, an edit, a message with no
    sender, and for bots, which cannot link an identity."""
    message = update.get("message")
    if not isinstance(message, dict):
        return []
    chat = message.get("chat") or {}
    chat_type = chat.get("type")
    if chat_type not in GROUP_CHAT_TYPES or chat.get("id") is None:
        return []
    people = [message.get("from") or {}] + list(message.get("new_chat_members") or [])
    seen: list[tuple[str, str, str]] = []
    for person in people:
        if (
            not isinstance(person, dict)
            or person.get("id") is None
            or person.get("is_bot")
        ):
            continue
        entry = (str(chat_type), str(chat["id"]), str(person["id"]))
        if entry not in seen:
            seen.append(entry)
    return seen


def group_message_of(update: dict):
    """The sender alone — the first of `group_members_of`, or None."""
    people = group_members_of(update)
    return people[0] if people else None


async def observe(
    conn, *, chat_type: str, external_ref: str, telegram_user_id: str
) -> StartResult:
    """One person seen speaking in one group. Idempotent."""
    user_id = await identity.user_for_identity(
        conn, provider=PROVIDER, external_id=telegram_user_id
    )
    if user_id is None:
        return StartResult(outcome="unknown_identity", handled=False)
    # A disabled account keeps its memberships (053) but is minted no new
    # ones. `users` is user-plane and readable here; the door holds no grant
    # on it, so this is the caller's refusal.
    state = await readers.row(conn, "SELECT state FROM users WHERE id = :u", u=user_id)
    if state is None or state["state"] != "active":
        return StartResult(outcome="user_inactive", handled=False)
    try:
        channel = bindings.channel_for_chat_type(chat_type)
    except bindings.BindingRefused as exc:
        return StartResult(outcome=exc.reason, handled=False)
    row = (
        await conn.execute(
            text(
                "SELECT o_workspace_id, o_outcome"
                "  FROM fn_group_member_seen(:ch, :ref, CAST(:u AS uuid))"
            ),
            {"ch": channel, "ref": external_ref, "u": user_id},
        )
    ).first()
    outcome = row[1] if row is not None else "unbound_chat"
    if outcome == "joined":
        logger.info(
            "membership sync: user %s joined workspace %s via chat %s",
            user_id,
            row[0],
            external_ref,
        )
    return StartResult(outcome=outcome, handled=outcome == "joined")
