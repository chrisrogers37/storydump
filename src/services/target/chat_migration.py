"""A group that became a supergroup keeps its workspace (#743).

Telegram retires a group's chat id when the group is upgraded to a supergroup,
and says so as it happens with two service messages: ``migrate_to_chat_id`` in
the old group, naming its successor, and ``migrate_from_chat_id`` in the new
supergroup, naming its predecessor. Both reach the ingress —
`vocabulary.ALLOWED_UPDATES` asks for ``message``, and a bot receives service
messages whatever its privacy mode.

Everything inbound resolves through the binding's ``external_ref``
(`tenant_resolution.resolve_chat`), so until the binding follows the chat,
whatever arrives from the new id — a tap, a member speaking — resolves to no
workspace. :func:`follow` moves the binding when Telegram says the chat moved.

The deliverer follows a moved chat too (`work_loop`'s sender hold), but only
when a send to the old id comes back refused with the successor named: at the
workspace's next delivery, and that delivery is the one that fails. It stays
the backstop for a notice this never saw. Both apply one rule,
`bindings.follow_or_retire`.

Why the notice can be acted on: the pair is Telegram's own — a service message
is minted by Telegram, not typed by a person, and the delivery reached the
dispatcher past the webhook's secret token. The write is scoped by the old id
itself: it resolves to at most one binding (`uq_binding_external`), whose
workspace becomes the transaction's tenant, so no other workspace's row can be
reached, and a successor another binding already holds is refused by the same
constraint and the old binding retired instead.

Nothing is said in the chat — a migration is not a conversation, the join
path's rule — and the notice's sender is not observed as a member here; they
are seen the next time they speak.
"""

from __future__ import annotations

import logging
from typing import Optional

from src.exceptions.tenancy import TenantResolutionError
from src.services.target import bindings, tenant_resolution, unit_of_work
from src.services.target.start_router import StartResult

logger = logging.getLogger(__name__)

#: Only a group migrates, so the id it retires is always a group binding's.
CHANNEL = bindings.channel_for_chat_type("group")

#: The binding now holds the successor id.
FOLLOWED = "chat_followed"
#: The successor is already another binding's; the old binding was revoked.
RETIRED = "chat_retired"


def migration_of(update: dict) -> Optional[tuple[str, str]]:
    """``(old_ref, new_ref)`` when *update* is either migration notice, else
    None. Text, because that is the binding's spelling of a chat id."""
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    chat_id = (message.get("chat") or {}).get("id")
    if chat_id is None:
        return None
    if message.get("migrate_to_chat_id") is not None:
        return str(chat_id), str(message["migrate_to_chat_id"])
    if message.get("migrate_from_chat_id") is not None:
        return str(message["migrate_from_chat_id"]), str(chat_id)
    return None


async def follow(conn, *, old_ref: str, new_ref: str) -> StartResult:
    """Move the binding that holds *old_ref* to *new_ref*, in the delivery's
    own transaction.

    The old id resolves through the resolver door — the ingress's pre-context
    path to a binding — and that binding's workspace becomes the transaction's
    tenant, with the system as the actor: nobody commanded the move, Telegram
    reported it, and the governance audit trigger on `channel_bindings`
    refuses an anonymous write.

    Idempotent across the pair and any redelivery: once one notice has moved
    the binding, the old id resolves to nothing and the other notice is a named
    no-op. Never raises for the update itself — the delivery is admitted by
    the time this runs (the dispatcher's rule); a database fault is the
    route's.
    """
    try:
        tenant = await tenant_resolution.resolve_chat(conn, CHANNEL, old_ref)
    except TenantResolutionError as exc:
        # Never bound, already followed (the other notice, or the deliverer got
        # there first), or revoked: no active binding holds the old id.
        logger.info(
            "chat migration %s -> %s: nothing to follow (%s)",
            old_ref,
            new_ref,
            exc.reason,
        )
        return StartResult(outcome=f"migration_{exc.reason}", handled=False)
    await unit_of_work.apply_gucs(
        conn, tenant_id=tenant.workspace_id, actor_kind="system", channel="telegram"
    )
    try:
        followed = await bindings.follow_or_retire(
            conn, binding_id=tenant.channel_binding_id, successor=new_ref
        )
    except bindings.BindingRefused as exc:
        logger.warning(
            "chat migration %s -> %s: successor refused (%s); binding %s unchanged",
            old_ref,
            new_ref,
            exc.reason,
            tenant.channel_binding_id,
        )
        return StartResult(outcome=f"migration_{exc.reason}", handled=False)
    if followed:
        logger.info(
            "chat migration %s -> %s: binding %s of workspace %s followed",
            old_ref,
            new_ref,
            tenant.channel_binding_id,
            tenant.workspace_id,
        )
        return StartResult(outcome=FOLLOWED, handled=True)
    logger.warning(
        "chat migration %s -> %s: the new id is another binding's; binding %s of"
        " workspace %s revoked",
        old_ref,
        new_ref,
        tenant.channel_binding_id,
        tenant.workspace_id,
    )
    return StartResult(outcome=RETIRED, handled=True)
