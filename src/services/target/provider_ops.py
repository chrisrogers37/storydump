"""The provider-operations permit rail (#860).

`ig` ONLY. Cloudinary is deliberately off the rail: upload and destroy are
recoverable effects, so their execution state lives in job attempts and
``audit_events``. Instagram publish is irreversible and customer-visible, so it
gets the full machinery.

**What the database enforces, stated exactly**, because the tempting
overstatement ("the DB stops the call before it happens") is not what happens:
the database denies permits to fenced workers (the lease CAS) and denies second
permits per generation (``uq_ops_business_key``). It does **not** stop an
already-permitted call — a permit holder whose lease expires *after* the permit
commits can still place its one authorized call. At-most-once survives because
that call was authorized exactly once and an unresolved publish permit is never
re-called, not because a stale call is physically prevented.

## Actor context is the caller's, and this rail does not invent it

Advancing ``publish_step`` is a governance write on ``post_intents``, so the
governance trigger requires ``app.actor_kind`` to be set on the connection. This
module deliberately does **not** set it: a service that invents an actor to get
past a governance guard has defeated the guard. The worker's unit of work
establishes it, and a caller who has not will get the trigger's own refusal —
"state change without app.actor_kind" — which is the correct failure.

## The transition-guard question (#860)

``jobs`` needs no transition guard. **That argument does not transfer.** On
the replayed schema ``provider_operations`` carries **zero** non-internal
triggers — no transition guard — and its constraints are
``uq_ops_business_key`` and a second UNIQUE constraint, three CHECKs, a
primary key and a foreign key.

Under the failure mode of #883 — two concurrent resolvers, both targeting
the **same** destination state, at ``read committed`` — the rowcounts are
**0 and 1**: the CAS discriminates a winner from a loser.

**Why the #883 failure does not occur here, precisely.** It needs a *trigger*
keyed on "did the value change" (``IS DISTINCT FROM``), which skips a same-state
write entirely and tells both writers they won. A CAS keyed on "is the value
still ``permitted``" matches nothing once the row has moved, so the loser gets
zero rows. Different mechanism, different outcome — which is why the ``jobs``
reasoning does not carry over.

**The residual difference, which is real and is why this module exists.** For
``jobs``, ``ready → leased`` is a UNIQUE INDEX: a writer who forgets the guard
still hits it, so the protection is writer-independent. Here the only guard on
``permitted → terminal`` is a predicate in application code, so a writer who
forgets it gets ``rowcount = 1`` and no error. The mechanism is equivalent in
**effect** and not equivalent in **robustness**. There is no trigger — this is
the service layer over the schema, and the CAS works — but the predicate is
made structurally unavoidable instead of remembered: every state-advancing
write goes through :func:`_advance`, and a gate test asserts no other module
updates ``provider_operations.state``. That is single-writer in the application
standing in for writer-independent at the database, and it is named as a
substitute rather than claimed as an equivalence.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.exceptions import StorydumpError
from src.services.target._dbapi import driver_candidates

logger = logging.getLogger(__name__)

#: Closed formats. One business key = at most one intended provider
#: effect, and `uq_ops_business_key` is the object that enforces it.
_KEY_FORMATS = {
    "container_create": "ig:container:{intent_id}:{generation}",
    "publish": "ig:publish:{intent_id}:{generation}",
}

#: The only provider on the rail (`ck_ops_provider` is closed to this too).
PROVIDER = "ig"

#: Terminal states a PERMIT may be resolved to — `provider_operations.state`,
#: not intent state. It shares its name with `intent_ledger.TERMINAL_STATES`
#: and shares no member with it: that one is the closed set of terminal
#: INTENT states, and the two cannot be folded together because they are two
#: sets, not two copies (#1325).
TERMINAL_STATES = ("succeeded", "failed", "ambiguous")

#: The insert is written WITHOUT `ON CONFLICT` on purpose: that clause has to
#: resolve its constraint at parse time, so a test that drops the guard to prove
#: it is load-bearing could not run at all. Letting the violation raise and
#: translating it here keeps the guard droppable and therefore provable.
_BUSINESS_KEY_GUARD = "uq_ops_business_key"


def _is_duplicate_business_key(exc: BaseException) -> bool:
    return any(
        getattr(c, "constraint_name", None) == _BUSINESS_KEY_GUARD
        for c in driver_candidates(exc)
    )


class PermitRefused(StorydumpError):
    """A permit or resolution was refused, with the reason NAMED.

    A count can say that a write lost but not why, and in the #883 shape it cannot
    tell a winner from a loser at all, so every refusal on this rail raises with a
    reason rather than returning a count for the caller to interpret. A caller
    that sees this must make no provider call and must not commit the work the
    permit would have authorized.
    """


def business_key(op_kind: str, intent_id, generation: int) -> str:
    """`ig:container:<intent>:<gen>` or `ig:publish:<intent>:<gen>`."""
    try:
        template = _KEY_FORMATS[op_kind]
    except KeyError:
        raise ValueError(f"not a rail op_kind: {op_kind!r}") from None
    return template.format(intent_id=intent_id, generation=generation)


async def _lease_is_live(conn, job_id, lease_token) -> bool:
    """The lease re-check — delegated to `jobs.lease_is_live`, the one
    SQL site for the predicate (state AND expiry AND ``FOR SHARE``; its
    docstring carries the derivation). The refusal stays this module's:
    a fenced permit raises :class:`PermitRefused`, not `JobFenced`."""
    from src.services.target.jobs import lease_is_live

    return await lease_is_live(conn, job_id, lease_token)


async def acquire_permit(
    conn,
    *,
    workspace_id,
    intent_id,
    job_id,
    lease_token,
    op_kind: str,
    generation: int = 1,
    archive_capability=None,
) -> dict[str, Any]:
    """Insert the permit and re-check the lease in ONE transaction, then commit.

    Returns the permit row. Raises :class:`PermitRefused` if the lease moved
    on, expired or was finalized, or if this generation already holds a permit.

    **The caller may issue the provider call only after this returns**, because
    only then has the permit committed. Crash before the commit leaves no
    permit and no call — a clean resume. Crash after it is what
    :func:`resume_unresolved` exists for.
    """
    if op_kind not in _KEY_FORMATS:
        raise ValueError(f"not a rail op_kind: {op_kind!r}")

    key = business_key(op_kind, intent_id, generation)
    try:
        from src.services.target import instagram_publish, readers

        provider = await readers.row(
            conn,
            "SELECT s.provider FROM post_intents i JOIN media_items m ON m.workspace_id=i.workspace_id AND m.id=i.media_item_id JOIN media_sources s ON s.workspace_id=m.workspace_id AND s.id=m.source_id WHERE i.workspace_id=:ws AND i.id=:id",
            ws=str(workspace_id),
            id=str(intent_id),
        )
        archive = None
        if provider is not None and provider["provider"] == "instagram_archive":
            archive = await instagram_publish.authorize(
                conn,
                workspace_id=workspace_id,
                intent_id=intent_id,
                capability=archive_capability,
                staged=True,
            )

        result = await conn.execute(
            text(
                "INSERT INTO provider_operations"
                " (workspace_id, intent_id, provider, op_kind, business_key,"
                "  generation, lease_token)"
                " VALUES (:ws, :intent, :provider, :kind, :key, :gen, :token)"
                " RETURNING id, state, generation, business_key"
            ),
            {
                "ws": str(workspace_id),
                "intent": str(intent_id),
                "provider": PROVIDER,
                "kind": op_kind,
                "key": key,
                "gen": generation,
                "token": str(lease_token),
            },
        )
        row = result.mappings().first()

        if op_kind == "publish":
            # The permit IS the durable record that a publish may have been
            # attempted, which is what makes `ck_ambiguous_called` exact.
            await conn.execute(
                text(
                    "UPDATE post_intents SET publish_step = 'publish_called'"
                    " WHERE id = :intent"
                ),
                {"intent": str(intent_id)},
            )

        if archive is not None:
            await instagram_publish.copy_permit(
                conn,
                operation_id=row["id"],
                workspace_id=workspace_id,
                intent_id=intent_id,
                subject=archive,
            )
        # The lease is the final lock and fresh wall-time check: waits on the
        # intent/operation/owned artifact cannot spend authorization's expiry.
        if not await _lease_is_live(conn, job_id, lease_token):
            raise PermitRefused(
                "fenced: no current live lease; no provider call may be made"
            )
        await conn.commit()
        return dict(row)
    except DBAPIError as exc:
        await conn.rollback()
        if _is_duplicate_business_key(exc):
            raise PermitRefused(
                f"duplicate permit: {key} already exists. One business key is at "
                "most one intended provider effect; a second call for the same "
                "generation is exactly what this rail forbids."
            ) from exc
        raise
    except Exception:
        await conn.rollback()
        raise


async def _advance(
    conn,
    *,
    op_id,
    to_state: str,
    response_ref: Optional[dict] = None,
    from_state: str = "permitted",
) -> None:
    """THE ONLY state-advancing write on this rail. See the module docstring.

    The ``AND state = :from_state`` predicate is the entire protection on this
    edge — there is no trigger and no unique index behind it. A zero-row result
    is a genuine refusal, never a no-op to shrug at, so it raises.
    """
    if to_state not in TERMINAL_STATES:
        raise ValueError(f"not a terminal op state: {to_state!r}")

    result = await conn.execute(
        text(
            "UPDATE provider_operations"
            " SET state = :to_state,"
            "     response_ref = COALESCE(CAST(:ref AS jsonb), response_ref)"
            " WHERE id = :op AND state = :from_state"
        ),
        {
            "op": str(op_id),
            "to_state": to_state,
            "from_state": from_state,
            "ref": None if response_ref is None else _as_json(response_ref),
        },
    )
    if result.rowcount == 0:
        raise PermitRefused(
            f"operation {op_id} was not in state {from_state!r}: another worker "
            f"resolved it first, or it was never permitted. Refusing to record "
            f"{to_state!r} over a resolution this worker did not make."
        )


def _as_json(payload: dict) -> str:
    import json

    return json.dumps(payload)


async def resolve_permit(
    conn, *, op_id, outcome: str, response_ref: Optional[dict] = None
) -> None:
    """Record the provider's answer."""
    await _advance(conn, op_id=op_id, to_state=outcome, response_ref=response_ref)


async def resolve_by_human(
    conn,
    *,
    op_id,
    outcome: str,
    verdict: str,
    actor_user_id: str,
    from_state: str = "ambiguous",
) -> None:
    """Record a HUMAN's answer for an op the provider never answered — the
    review card's resolutions: `succeeded` with verdict `posted`
    (the member sees the story), `failed` with `not_posted` (they do not; the
    intent is re-approved and a NEW generation may be permitted — never
    beside an unresolved one) or `given_up`. The verdict and who gave it ride
    `response_ref`, beside the reconciler's evidence if any; the op leaves
    the un-retirable class (`ix_ops_retire`)."""
    await _advance(
        conn,
        op_id=op_id,
        to_state=outcome,
        response_ref={"v": 1, "verdict": verdict, "by": str(actor_user_id)},
        from_state=from_state,
    )


async def resume_unresolved(conn, *, op: dict, intent_id) -> str:
    """A permit committed and no outcome was recorded. The op kinds DIVERGE.

    Returns what the caller may do next: ``"repermit"`` or ``"parked"``.

    The divergence is by blast radius rather than by
    symmetry:

    - ``container_create`` — an orphaned container is inert (nothing is
      published, containers expire unused, and Meta's caps count the publish
      step only). Mark the op ``failed`` with ``lost_response``, bump
      ``generation``, permit a fresh create. **Never intent-level ambiguity**:
      recoverable effects must not park the pipeline.
    - ``publish`` — **do not re-call**, ever. Mark the op ``ambiguous`` and the
      intent ``publishing_ambiguous``; the reconciler owns it from there, and
      ``generation`` never bumps out of ambiguity, which is what stops a second
      business key (and therefore a second permitted call) existing at all.
    """
    if op["op_kind"] == "container_create":
        await _advance(
            conn,
            op_id=op["id"],
            to_state="failed",
            response_ref={"v": 1, "error": "lost_response"},
        )
        return "repermit"

    await _advance(
        conn,
        op_id=op["id"],
        to_state="ambiguous",
        response_ref={"v": 1, "error": "lost_response"},
    )
    # Checked like `_advance` above: an intent that already left `publishing`
    # is not parked, and the raise takes the op's `ambiguous` mark back with
    # the caller's transaction (#1438).
    moved = (
        await conn.execute(
            text(
                "UPDATE post_intents SET state = 'publishing_ambiguous'"
                " WHERE id = :intent AND state = 'publishing' RETURNING id"
            ),
            {"intent": str(intent_id)},
        )
    ).first()
    if moved is None:
        raise ValueError(f"intent {intent_id} left 'publishing' before it was parked")
    return "parked"
