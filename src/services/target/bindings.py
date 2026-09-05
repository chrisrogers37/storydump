"""The `channel_bindings` writer (#1172, gate clause 4).

## What this closes

Measured on `main` @ `e057063`: **zero** `INSERT INTO channel_bindings`, by SQL
or by ORM — `ChannelBinding` has no reference outside its own module and the
package export. Positive-controlled against the sibling tenant tables, which
carry two INSERTs each, so the zero is the table's and not the probe's.

`06`/D13 ratifies `0..n` bindings per workspace as a deliberate widening of
#721's one-chat v1, and **zero has been delivered.** Everything downstream is
already built and inert for exactly this reason: `work_loop`'s sweep mints
`deliver_outbox` jobs `FROM channel_bindings`, the `deliver_outbox` handler
resolves a binding to its `external_ref`, and `outbox.enqueue` writes the rows
they carry. None of it can run without a row here.

## The one rule that is a product rule, and it is stated rather than inferred

**A chat cannot be double-bound.** `uq_binding_external UNIQUE (channel,
external_ref)` is global, not per-workspace, and the plan says so in prose —
`02` §6's existence-oracle inventory calls it *"inherent to the product (a chat
cannot be double-bound...)"*. So the cap is INTENDED. It is worth being
explicit that this was read rather than deduced: the constraint alone could
equally have been an artifact, and `0..n` is bindings-per-WORKSPACE, which is a
different axis from workspaces-per-CHAT.

FC-1.3's *"a Telegram account manages one-to-many workspaces"* is about the
IDENTITY, not the chat: one person with two chats is the supported shape, one
chat driving two workspaces is not.

## Why the upsert is guarded, measured rather than argued

Three forms were driven against the real schema as `svc_ingress`, with a chat
already bound to another workspace:

| form | result |
|---|---|
| plain `INSERT` | `UniqueViolation` on `uq_binding_external` |
| `ON CONFLICT DO UPDATE SET state='active'` | `InsufficientPrivilege` — RLS `USING` refuses |
| the same **guarded** by `workspace_id = EXCLUDED.workspace_id` | clean, `rowcount = 0` |

RLS does refuse the theft, so no form of this silently re-points a chat. What
the guard buys is a **refusal instead of an error**: the naive upsert fails as
a policy violation, which a caller can only render as a 500. The guarded form
lets the conflicting row stay invisible and reports "not yours" as an ordinary
outcome.

**On the existence oracle, stated precisely, because an earlier version of this
note over-claimed.** :data:`TAKEN` has exactly one cause — the conflict fired
and the guard rejected it — so it leaks *precisely* what the `UniqueViolation`
leaks: this chat is bound somewhere. The guarded form changes the SHAPE of that
answer (a value a caller can render, not an exception it can only turn into a
500); it does not change the INFORMATION. It neither widens nor narrows the
leak `02` §6 inventories.

That leak is accepted there on a stated ground — *"the person probing must
already be adding the bot to that chat"* — which bounds who can ask, not what
the answer reveals. Nothing in the plan names a rule about collapsing refusals
into one string; returning a single value for the single case is this module's
choice and should be reviewed as such.
"""

from __future__ import annotations

import re

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.services.target._dbapi import constraint_violated

from src.exceptions.base import StorydumpError

#: `ck_bindings_channel`, verbatim. A closed set the CALLER supplies, so it is
#: refused by name here rather than left to surface as a check violation.
CHANNELS: tuple[str, ...] = ("telegram_group", "telegram_dm")

#: A Telegram chat id as text — negative for groups and supergroups. Both
#: members of :data:`CHANNELS` are Telegram, which is what makes this shape
#: knowable; adding a non-Telegram channel to `ck_bindings_channel` must
#: revisit this, and the constraint moving is the signal to do so.
_EXTERNAL_REF = re.compile(r"^-?[0-9]{1,32}$")

#: A new row was written.
BOUND = "bound"
#: The workspace's own binding for this chat was re-activated. `02` §1's DDL
#: comment specifies this path: `uq_binding_external` holds across states, so
#: re-adding the bot flips `revoked` back to `active` and preserves history.
REBOUND = "rebound"
#: The chat belongs to a DIFFERENT workspace. Refused — see the module note.
TAKEN = "taken"


class BindingRefused(StorydumpError):
    """A caller-supplied value the boundary refuses, before the database sees
    it (`provisioning.ProvisioningRefused`'s shape)."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        super().__init__(
            f"binding refused: {reason}" + (f" — {detail}" if detail else "")
        )


#: Telegram's chat types → this module's channel vocabulary. **It lives here,
#: and that placement is the point.** The `/start` router (branden's lane) and
#: any future adapter both need it, and a mapping embedded in two lanes is one
#: that can disagree about what a supergroup is — the fork this module exists
#: to prevent for the write itself.
#:
#: A broadcast `channel` is deliberately absent rather than mapped: it is a
#: real Telegram chat type with no value in `ck_bindings_channel`, and guessing
#: one would invent a product decision. It refuses by name.
_CHAT_TYPES: dict[str, str] = {
    "private": "telegram_dm",
    "group": "telegram_group",
    "supergroup": "telegram_group",
}


def channel_for_chat_type(chat_type: object) -> str:
    """One Telegram `chat.type` → one `ck_bindings_channel` value.

    The legacy handler tested `chat.type not in ("group", "supergroup")` inline
    and had no DM path at all; this is that rule made total and named, so a
    caller cannot quietly omit a type.
    """
    mapped = _CHAT_TYPES.get(chat_type) if isinstance(chat_type, str) else None
    if mapped is None:
        raise BindingRefused(
            "chat_type_unsupported", f"{chat_type!r} has no channel_bindings value"
        )
    return mapped


def _clean(chat_type: object, external_ref: object) -> tuple[str, str]:
    """Telegram's own vocabulary in, this schema's out.

    **The writers take the RAW `chat.type`, and that is the contract rather
    than a convenience.** The `/start` router passes what Telegram sent
    precisely because the mapping is this module's domain rule; a writer that
    demanded an already-mapped value would leave the rule owned here and
    implemented nowhere, which is the state #1178 was reviewed in.
    """
    channel = channel_for_chat_type(chat_type)
    if not isinstance(external_ref, str) or not external_ref.strip():
        raise BindingRefused("external_ref_required")
    ref = external_ref.strip()
    if not _EXTERNAL_REF.match(ref):
        raise BindingRefused("external_ref_malformed", "a Telegram chat id")
    return channel, ref


async def bind(session, *, workspace_id: str, chat_type: str, external_ref: str) -> str:
    """Bind one chat to one workspace. Returns :data:`BOUND`, :data:`REBOUND`
    or :data:`TAKEN`.

    Runs in the caller's transaction — a binding created by the delivery that
    announced it must roll back with it, the same rule `outbox.enqueue` states.

    **`TAKEN` is not an exception**, because it is not a fault: adding the bot
    to a chat another workspace already uses is a thing a person can do by
    accident, and the caller has to render it. The two error-shaped outcomes it
    replaces are in the module note.

    New versus re-activated is read from `xmax = 0` on the RETURNING row —
    `provisioning.create_destination`'s idiom, one round trip rather than a
    read-then-write that another transaction could interleave.
    """
    channel, ref = _clean(chat_type, external_ref)
    row = (
        await session.execute(
            text(
                "INSERT INTO channel_bindings (workspace_id, channel, external_ref)"
                " VALUES (:ws, :ch, :ref)"
                " ON CONFLICT (channel, external_ref) DO UPDATE"
                "    SET state = 'active'"
                "  WHERE channel_bindings.workspace_id = EXCLUDED.workspace_id"
                " RETURNING (xmax = 0) AS created"
            ),
            {"ws": str(workspace_id), "ch": channel, "ref": ref},
        )
    ).first()
    if row is None:
        return TAKEN
    return BOUND if row[0] else REBOUND


async def revoke(
    session, *, workspace_id: str, chat_type: str, external_ref: str
) -> bool:
    """Mark this workspace's binding revoked — the bot was kicked or blocked.

    `02` §1: `revoked` is *"written by the adapter on the my_chat_member
    event"*, and the row is KEPT rather than deleted so :func:`bind` can flip
    it back and the history survives. Returns whether a row moved; False means
    the workspace has no such binding, which is the correct answer for a kick
    from a chat it never held.
    """
    channel, ref = _clean(chat_type, external_ref)
    result = await session.execute(
        text(
            "UPDATE channel_bindings SET state = 'revoked'"
            " WHERE workspace_id = :ws AND channel = :ch AND external_ref = :ref"
            "   AND state <> 'revoked'"
        ),
        {"ws": str(workspace_id), "ch": channel, "ref": ref},
    )
    return result.rowcount > 0


async def revoke_by_id(session, *, binding_id: str) -> bool:
    """Mark one binding revoked by its id — the deliverer's spelling, for the
    moment a send comes back "chat gone" (the bot kicked or blocked, the chat
    deleted). Same row-kept semantics as :func:`revoke`; the sweep stops
    minting for it on its next pass (`work_loop`: `b.state = 'active'`)."""
    result = await session.execute(
        text(
            "UPDATE channel_bindings SET state = 'revoked'"
            " WHERE id = :b AND state <> 'revoked'"
        ),
        {"b": str(binding_id)},
    )
    return result.rowcount > 0


async def repoint(session, *, binding_id: str, external_ref: str) -> bool:
    """A group became a supergroup: Telegram retires the old chat id and names
    the new one. The binding follows the chat. If the new id is already
    another binding's, the row is revoked instead — `uq_binding_external`
    still holds one chat to one workspace."""
    _, ref = _clean("supergroup", external_ref)
    try:
        result = await session.execute(
            text(
                "UPDATE channel_bindings SET external_ref = :ref, channel = 'telegram_group'"
                " WHERE id = :b AND state <> 'revoked'"
            ),
            {"b": str(binding_id), "ref": ref},
        )
    except DBAPIError as exc:
        if constraint_violated(exc, "uq_binding_external"):
            return False
        raise
    return result.rowcount > 0
