"""The one place a service writes an `audit_events` row directly.

The triggers own state-change audit (`trg_governance_audit`,
`trg_intent_audit`). Four writers need a row the triggers cannot produce — a
`cap_deferred`, a float wait, a `revoke_failed`, a parked drain — and each
wrote the nine-column INSERT out by hand, two of them disagreeing on how the
actor is recorded. The columns are one statement here; the actor spelling is a
named fragment, because the two spellings are a real difference and not a
drift.

`src/api/routes/v1.py`'s `cli_command` row is the documented API-side
exception and stays where it is: it is the API tier's, not this one's.

What a caller keeps: its own transaction. Two of the four open one
deliberately (`credential_lifecycle._audit_revoke_failed`,
`offboarding._audit`) so the record survives the raise that follows;
:func:`record` takes the executor and writes one statement, and commits
nothing.
"""

from __future__ import annotations

import json

from sqlalchemy import text

#: Read the actor from the transaction's GUCs, exactly as the triggers do.
#: The default, and what three of the four writers use.
ACTOR_FROM_GUCS = (
    "current_setting('app.actor_kind'),"
    " NULLIF(current_setting('app.actor_user_id', true), '')::uuid,"
    " NULLIF(current_setting('app.channel', true), '')"
)

#: A worker session that sets no `app.channel` (`work_loop.poller_session_factory`),
#: naming the channel itself so the row still says where the write came from —
#: a NULL there would lose the only thing the row says about who ran the drain.
#: `offboarding`'s parked-drain record is the one writer of this shape.
ACTOR_SYSTEM_CHANNEL = "current_setting('app.actor_kind'), NULL, 'system'"


async def record(
    executor,
    *,
    workspace_id: str,
    entity_kind: str,
    entity_id_sql: str,
    from_state: str,
    to_state: str,
    detail: dict,
    actor_sql: str = ACTOR_FROM_GUCS,
    **params,
) -> None:
    """One `audit_events` row, in the CALLER's transaction.

    *entity_id_sql* is a bind name or a cast of one (``":intent"``,
    ``"CAST(:ws AS uuid)"``), supplied by the writer — never by a caller of
    the writer, and never built from user input. Its bind rides in *params*.

    `entity_kind`, `from_state` and `to_state` are bound rather than spliced:
    they are `Text` columns (`models/target/intent_ledger.AuditEvent`), and
    `v1.py`'s row already binds `:entity_kind`.
    """
    await executor.execute(
        text(
            "INSERT INTO audit_events (workspace_id, entity_kind, entity_id,"
            " from_state, to_state, actor_kind, actor_user_id, channel, detail)"
            f" VALUES (:ws, :entity_kind, {entity_id_sql},"
            f"         :from_state, :to_state, {actor_sql},"
            "         CAST(:detail AS jsonb))"
        ),
        {
            "ws": workspace_id,
            "entity_kind": entity_kind,
            "from_state": from_state,
            "to_state": to_state,
            "detail": json.dumps(detail),
            **params,
        },
    )
