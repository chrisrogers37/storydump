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

It also does not widen the existence oracle `02` §6 already accepts and bounds
— a plain INSERT raises and thereby confirms the chat is bound *somewhere*,
while this returns the same :data:`TAKEN` whether the chat is held elsewhere or
the caller simply may not have it.
"""

from __future__ import annotations

import re

from sqlalchemy import text

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


def _clean(channel: object, external_ref: object) -> tuple[str, str]:
    if channel not in CHANNELS:
        raise BindingRefused("channel_unknown", f"one of {', '.join(CHANNELS)}")
    if not isinstance(external_ref, str) or not external_ref.strip():
        raise BindingRefused("external_ref_required")
    ref = external_ref.strip()
    if not _EXTERNAL_REF.match(ref):
        raise BindingRefused("external_ref_malformed", "a Telegram chat id")
    return str(channel), ref


async def bind(session, *, workspace_id: str, channel: str, external_ref: str) -> str:
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
    channel, ref = _clean(channel, external_ref)
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
    session, *, workspace_id: str, channel: str, external_ref: str
) -> bool:
    """Mark this workspace's binding revoked — the bot was kicked or blocked.

    `02` §1: `revoked` is *"written by the adapter on the my_chat_member
    event"*, and the row is KEPT rather than deleted so :func:`bind` can flip
    it back and the history survives. Returns whether a row moved; False means
    the workspace has no such binding, which is the correct answer for a kick
    from a chat it never held.
    """
    channel, ref = _clean(channel, external_ref)
    result = await session.execute(
        text(
            "UPDATE channel_bindings SET state = 'revoked'"
            " WHERE workspace_id = :ws AND channel = :ch AND external_ref = :ref"
            "   AND state <> 'revoked'"
        ),
        {"ws": str(workspace_id), "ch": channel, "ref": ref},
    )
    return result.rowcount > 0


async def active_binding_ids(session, *, workspace_id: str) -> list[str]:
    """This workspace's deliverable bindings, for a producer deciding what an
    EMPTY set means.

    That decision is deliberately the producer's and not this function's:
    `outbox.UNDELIVERABLE` exists because "nobody to tell" was being recorded
    as a clean run, and a helper that resolved AND iterated would make the
    empty case a zero-length loop again.
    """
    rows = (
        await session.execute(
            text(
                "SELECT id FROM channel_bindings"
                " WHERE workspace_id = :ws AND state = 'active'"
                " ORDER BY created_at, id"
            ),
            {"ws": str(workspace_id)},
        )
    ).all()
    return [str(r[0]) for r in rows]
