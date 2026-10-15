"""The intent ledger service.

The database enforces the state machine in `post_intent_transitions` through
`trg_intent_guard`, `trg_intent_no_self_transition`, `trg_intent_audit` and
`trg_intent_insert_guard`, defined in `src/models/target/ddl/`.

This module issues an UPDATE and translates a database refusal into a typed
error. It keeps no Python copy of the legal edges. `legal_transitions` reads
the edge table for display, never to validate a write.

Terminal rows reject every update. A write naming `state` rejects the current
state, while a checkpoint that leaves `state` unnamed can update a live row.
Audit triggers require an actor and treat both NULL and an empty actor GUC
as unset, including on pooled connections.
"""

from __future__ import annotations

from typing import Optional

from asyncpg.exceptions import CheckViolationError, RaiseError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.config.defaults import DEFAULT_REPOST_TTL_DAYS
from src.exceptions.base import StorydumpError
from src.services.target import audit, vocabulary
from src.services.target._dbapi import driver_error_is


class IntentNotVisible(StorydumpError):
    """The UPDATE matched no row: the intent does not exist, or row-level
    security hides it from this session's tenant.

    A caller's bug, not a ledger refusal, so it is deliberately not an
    :class:`IntentTransitionRefused`: the callers that catch a refusal and
    carry on (the prompt sweep, the offboarding drain) do not swallow it.
    """


class IntentTransitionRefused(StorydumpError):
    """The database refused a transition: a `check_violation` from a guard or a
    state-completeness CHECK, or the audit trigger's refusal of an actor-less
    change.

    Carries the driver's message rather than a reworded one: it names which
    rule fired (terminal-immutable, illegal edge, same-state write, incomplete
    row, missing actor), and a friendlier string here would lose the
    distinction the operator needs.

    Raised after Postgres has aborted the transaction: a caller that means to
    go on after catching it runs :func:`transition` inside
    `session.begin_nested()` and catches it outside that block, so the
    savepoint rolls back, as the prompt sweep and the offboarding drain do.
    """


async def legal_transitions(session) -> set:
    """The edge set, read from `post_intent_transitions`.

        For display only, and nothing displays it: the one caller is
        `tests/scripts/test_intent_ledger_gate.py`, which uses it to prove the edge
        set is READ from `post_intent_transitions` rather than kept in Python.
    Never call this to pre-validate a write — see the
        module docstring on why a second authority is the thing this module avoids.
    """
    rows = await session.execute(
        text("SELECT from_state, to_state FROM post_intent_transitions")
    )
    return {(r[0], r[1]) for r in rows}


#: The terminal intent states — the closed set no edge leaves. ONE Python
#: home (`vocabulary`, where the CLI reads it too; this is its name for the
#: services), because two spellings of the set have to AGREE for a
#: workflow to converge at all: `offboarding.drain` selects `NOT terminal` and
#: cancels it,
#: and `fn_offboard_finalize` (`04_functions.sql`) refuses while any `NOT terminal` row
#: survives, so a state one list carries and the other does not is an offboard
#: that mints successors forever. The database is still the authority — this is
#: a name for `trg_intent_guard`'s set, not a second one.
TERMINAL_STATES: tuple[str, ...] = vocabulary.TERMINAL_STATES

#: A story still in flight, as SQL on a bare `state`: the predicate of
#: `uq_intent_live_subject` (one live story per item and account), which is
#: how an INSERT names that index as its conflict arbiter, and the set a
#: writer flags or drains. Spelled from TERMINAL_STATES, never typed again.
NOT_TERMINAL = "state NOT IN (" + ", ".join(f"'{s}'" for s in TERMINAL_STATES) + ")"

#: The live story of one item on one account: `uq_intent_live_subject`'s key
#: and predicate, as a WHERE on bare columns with ``:ws``, ``:media`` and
#: ``:acct`` bound. At most one row matches.
LIVE_SUBJECT = (
    "workspace_id = :ws AND media_item_id = :media AND ig_account_id = :acct"
    f" AND {NOT_TERMINAL}"
)

#: An account a story may still post to, as SQL on the account's alias ``a``:
#: one spelling for the schedule verbs' account read, the serve and miss doors,
#: the successor trigger (`intent repeat constraints`), which the schedule gate pins against their
#: bodies, and the calendar's projection of a series (`upcoming`).
LIVE_ACCOUNT = (
    "a.state IN (" + ", ".join(f"'{s}'" for s in vocabulary.LIVE_ACCOUNT_STATES) + ")"
)


def repeat_sql(prefix: str = "") -> str:
    """A story's repeat rule as one value, NULL for none (`intent repeat constraints`): the one
    shape every read and every answer carries, ``{unit, every, days, at}``
    with ``at`` as ``HH:MM``. *prefix* is the table's alias (``"i."``), or
    nothing for a bare row (a RETURNING)."""
    p = prefix
    return (
        f"CASE WHEN {p}repeat_unit IS NOT NULL"
        f" THEN jsonb_build_object('unit', {p}repeat_unit, 'every', {p}repeat_every,"
        f" 'days', to_jsonb({p}repeat_days), 'at', to_char({p}repeat_at, 'HH24:MI'))"
        " END"
    )


#: The `last_error->'evidence'` MERGE, as a fragment. `reconciler`'s own prose
#: says this must never be a rebuild: `evidence` carries `checks`,
#: `last_checked_at` and the trail, which is the operator's entire inheritance
#: on a parked intent, and a `jsonb_build_object` rewrite of it notifies the
#: customer by destroying the evidence. Four writers spelled it out; `{seed}`
#: is what an absent `last_error` becomes, `{key}` and `{value}` are SQL
#: literals chosen by the writer — never caller input. The guards, the WHERE
#: and any RETURNING stay with the writer: the four differ there, and a
#: function that took them all as parameters would be a second statement
#: builder rather than one shared clause.
EVIDENCE_MERGE = (
    "last_error = COALESCE(last_error, CAST('{seed}' AS jsonb))"
    " || jsonb_build_object('evidence',"
    "      COALESCE(last_error->'evidence', CAST('{{}}' AS jsonb))"
    "      || jsonb_build_object('{key}', {value}))"
)


async def transition(
    session, intent_id: str, to_state: str, *, approval_rule: Optional[str] = None
) -> None:
    """Move an intent to *to_state*, or raise.

    No pre-check. The UPDATE goes out and the triggers decide; a refusal comes
    back as :class:`IntentTransitionRefused`. A refusal is a Postgres error, so
    it aborts the caller's transaction: to go on after catching one, call this
    inside `session.begin_nested()` and catch the refusal outside that block,
    so the savepoint rolls back.

    *approval_rule* is the automatic approval's stamp: the serve step
    passes it with `approved`, and the same UPDATE writes it, because
    `tg_intent_person_or_rule` (`approval position constraints`) admits a rule only on the approval it
    names. This is the column's one writer.

    The caller must be inside a unit of work that has set `app.actor_kind` —
    `trg_intent_audit` refuses an anonymous state change, and this module does
    not duplicate that check for the same reason it does not duplicate the edge
    set.

    An UPDATE that matches no row (an id that does not exist, or a row this
    session's tenant cannot see) raises :class:`IntentNotVisible` instead of
    passing as a success.
    """
    sets, params = "state = :s", {"s": to_state, "i": intent_id}
    if approval_rule is not None:
        sets, params["r"] = "state = :s, approval_rule = :r", approval_rule
    try:
        result = await session.execute(
            text(f"UPDATE post_intents SET {sets} WHERE id = :i"), params
        )
    except DBAPIError as exc:
        # Both driver shapes, because the triggers use both deliberately:
        # `trg_intent_guard` raises WITH `ERRCODE = 'check_violation'`
        # (asyncpg surfaces `CheckViolationError`), while the audit and insert
        # guards use a bare `RAISE EXCEPTION`, which arrives as `RaiseError`
        # (P0001). Catching only one of them lets the illegal-transition case
        # escape as a raw IntegrityError.
        refusal = driver_error_is(exc, RaiseError, CheckViolationError)
        if refusal is not None:
            raise IntentTransitionRefused(str(refusal)) from exc
        raise
    if result.rowcount == 0:
        raise IntentNotVisible(
            f"post_intent {intent_id} matched no row: it does not exist, or this"
            " session's tenant cannot see it"
        )


async def settlement(session, *, workspace_id: str, intent_id: str) -> dict:
    """What a card in any state past `awaiting_approval` says about itself:
    the state, who last moved it and when — the newest `audit_events` row for
    the intent that MOVED it (`ix_audit_entity`, bound on `workspace_id`;
    `audit.moved`), or the row's own `entered_state_at` when no audit row
    exists (a clock or reaper move records `actor_user_id` NULL).
    ``approval_rule`` is the rule that approved it, None for a person:
    a line names the mechanism with it (`prompts.outcome_line`)."""
    row = (
        (
            await session.execute(
                text(
                    "SELECT i.state, i.published_via, i.entered_state_at,"
                    "       i.approval_rule, a.actor_user_id, a.created_at"
                    "  FROM post_intents i"
                    "  LEFT JOIN LATERAL ("
                    "    SELECT actor_user_id, created_at FROM audit_events e"
                    "     WHERE e.workspace_id = i.workspace_id"
                    "       AND e.entity_kind = 'post_intent' AND e.entity_id = i.id"
                    f"       AND {audit.moved('e')}"
                    "     ORDER BY e.id DESC LIMIT 1) a ON true"
                    " WHERE i.id = :i AND i.workspace_id = :ws"
                ),
                {"i": str(intent_id), "ws": str(workspace_id)},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return {
            "state": None,
            "published_via": None,
            "approval_rule": None,
            "by_user_id": None,
            "at": None,
        }
    return {
        "state": row["state"],
        "published_via": row["published_via"],
        "approval_rule": row["approval_rule"],
        "by_user_id": None
        if row["actor_user_id"] is None
        else str(row["actor_user_id"]),
        "at": row["created_at"] or row["entered_state_at"],
    }


async def current_state(session, intent_id: str) -> Optional[str]:
    """The intent's state, or None if it does not exist."""
    row = (
        await session.execute(
            text("SELECT state FROM post_intents WHERE id = :i"), {"i": intent_id}
        )
    ).first()
    return row[0] if row else None


def resolve_repost_ttl_days(ttl_days: Optional[int]) -> int:
    """The posting fallback, zero-to-default included."""
    return int(ttl_days or DEFAULT_REPOST_TTL_DAYS)


async def posted_effects(
    session,
    *,
    workspace_id: str,
    media_item_id: str,
    ig_account_id: str,
    intent_id: str,
    ttl_days: Optional[int],
    created_by_user_id: Optional[str] = None,
) -> None:
    """What a post leaves beside the `posted` flip, in
    ONE spelling for its three writers — the manual path (`mark_posted`),
    the pipeline's confirm and the review card's "it posted": the media's
    count and last-posted stamp, the repost lock keyed to the account
    (created by the intent, and by the person when a person did it), the
    account's last-posted stamp. *ttl_days* falls back to the product
    default."""
    await session.execute(
        text(
            "UPDATE media_items SET times_posted = times_posted + 1,"
            " last_posted_at = now() WHERE id = :media"
        ),
        {"media": str(media_item_id)},
    )
    await session.execute(
        text(
            "INSERT INTO post_locks (workspace_id, media_item_id, kind,"
            " ig_account_id, expires_at, created_by_intent_id, created_by_user_id)"
            " VALUES (:ws, :media, 'recent', :acct,"
            "         now() + make_interval(days => :ttl_days), :intent, :u)"
            " ON CONFLICT (workspace_id, media_item_id, kind, ig_account_id)"
            "   WHERE ig_account_id IS NOT NULL"
            " DO UPDATE SET expires_at = EXCLUDED.expires_at"
        ),
        {
            "ws": str(workspace_id),
            "media": str(media_item_id),
            "acct": str(ig_account_id),
            "ttl_days": resolve_repost_ttl_days(ttl_days),
            "intent": str(intent_id),
            "u": created_by_user_id,
        },
    )
    await session.execute(
        text("UPDATE ig_accounts SET last_posted_at = now() WHERE id = :acct"),
        {"acct": str(ig_account_id)},
    )
