"""L.1 — the intent ledger service (#858, `04` §L.1).

A deliberately thin layer over `post_intents`. Everything that *enforces*
anything lives in the database: migration `055` ships `trg_intent_guard`,
`trg_intent_audit` and `trg_intent_insert_guard`, and `04` §L.1's gate says
outright that **the trigger, not the service, is the authority**.

## Why this module validates nothing

The obvious convenience here is to load the legal edges once and check a
transition in Python before issuing the UPDATE — friendlier errors, one fewer
round trip. **That would be a second authority**, and the failure it invites is
not hypothetical: the Python copy and `post_intent_transitions` drift, the
service starts refusing a transition the database allows (or worse, permitting
one it forbids), and every test that goes through the service keeps passing
because it is asserting the copy.

So this module issues the UPDATE and lets the trigger decide. Its only job
beyond that is to translate the database's `check_violation` into a typed
error, which is a *presentation* concern and cannot disagree with the
authority.

:func:`legal_transitions` exists for callers that need to *show* the edge set —
a UI, an operator report. It reads the table. It is never consulted before a
write, and no caller should make it so.

## Which guarantees are the database's, measured rather than assumed

Probed against a replayed target schema before this module was written, because
the gate's phrasing and the trigger's behaviour are not identical:

* **Terminal rows reject EVERY update**, not only state changes — an attempt to
  set `ig_permalink` on a `cancelled` intent raises. The guard tests
  `OLD.state` first, before it looks at what changed.
* **A same-state update is a no-op, not a refusal.** `scheduled → scheduled`
  succeeds. The guard only compares when `NEW.state IS DISTINCT FROM
  OLD.state`, so the "double transition" the gate names is prevented
  *structurally* — after A→B you are no longer in A, so the edge no longer
  matches — rather than by rejecting a repeat. The same-state write also
  produces **no audit row** (the AFTER trigger carries `WHEN OLD.state IS
  DISTINCT FROM NEW.state`) and does **not** advance `entered_state_at`, so it
  cannot forge a second ledger entry. That is the property worth relying on,
  and it is what the gate asserts.
* **An actor-less state change raises**, from `trg_intent_audit`.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import text

from src.exceptions.base import StorydumpError


class IntentTransitionRefused(StorydumpError):
    """The database refused a transition. Raised from its `check_violation`.

    Carries the driver's message rather than a reworded one: the trigger names
    which rule fired (terminal-immutable vs illegal-edge) and a friendlier
    string here would lose the distinction the operator needs.
    """


async def legal_transitions(session) -> set:
    """The edge set, read from `post_intent_transitions`.

    For display only. Never call this to pre-validate a write — see the module
    docstring on why a second authority is the thing this module avoids.
    """
    rows = await session.execute(
        text("SELECT from_state, to_state FROM post_intent_transitions")
    )
    return {(r[0], r[1]) for r in rows}


async def transition(session, intent_id: str, to_state: str) -> None:
    """Move an intent to *to_state*, or raise.

    No pre-check. The UPDATE goes out and `trg_intent_guard` decides; a
    `check_violation` comes back as :class:`IntentTransitionRefused`.

    The caller must be inside a unit of work that has set `app.actor_kind` —
    `trg_intent_audit` refuses an anonymous state change, and this module does
    not duplicate that check for the same reason it does not duplicate the edge
    set.
    """
    # Both driver shapes, because the triggers use both deliberately:
    # `trg_intent_guard` raises WITH `ERRCODE = 'check_violation'` (asyncpg
    # surfaces `CheckViolationError`), while the audit and insert guards use a
    # bare `RAISE EXCEPTION`, which arrives as `RaiseError` (P0001). Catching
    # only one of them was the first version's bug — the illegal-transition
    # case escaped as a raw IntegrityError.
    from asyncpg.exceptions import (  # noqa: PLC0415 — driver-specific
        CheckViolationError,
        RaiseError,
    )
    from sqlalchemy.exc import DBAPIError

    from src.services.target._dbapi import driver_candidates

    try:
        await session.execute(
            text("UPDATE post_intents SET state = :s WHERE id = :i"),
            {"s": to_state, "i": intent_id},
        )
    except DBAPIError as exc:
        refusals = [
            c
            for c in driver_candidates(exc)
            if isinstance(c, (RaiseError, CheckViolationError))
        ]
        if refusals:
            raise IntentTransitionRefused(str(refusals[0])) from exc
        raise


async def current_state(session, intent_id: str) -> Optional[str]:
    """The intent's state, or None if it does not exist."""
    row = (
        await session.execute(
            text("SELECT state FROM post_intents WHERE id = :i"), {"i": intent_id}
        )
    ).first()
    return row[0] if row else None
