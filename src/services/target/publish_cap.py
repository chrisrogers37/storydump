"""L.5 — the §4 cap ledger: the atomic flip, the manual post, the refund, and
the two resolve edges (#862, `02` §4).

The cap-ledger core, separate from the pipeline body because it is what the
product depends on: **zero over-cap** — posting more than Meta allows gets the
account limited. Every function here is service-orchestrated raw SQL (there is
no door), run inside the caller's `UnitOfWork` transaction (tenant + actor
GUCs already set by the UoW), on the L.1 doctrine that the database is the
authority: the four `post_intents` CHECKs, `ck_dpc_nonneg`, the transition
trigger, and `uq_publish_exclusive` (key 4) enforce; this module orchestrates
and reads back the row counts the enforcement returns.

## The flip is ONE statement, and that is the whole safety argument

`approved → publishing` is a single CTE (`02` §4): the cap debit and the state
flip are coupled so a zero-row flip cannot leave a committed debit behind — the
pass-2 two-statement form could consume a cap slot without entering
`publishing` (R2). The debit's `ON CONFLICT … WHERE d.count < d.cap_at_write`
denies at the cap atomically — no check-then-act gap — so N concurrent flips
resolve to exactly `cap` proceeds and the rest deferrals, at READ COMMITTED,
with no over-cap. `key 4` (`uq_publish_exclusive` on `provider_account_ref`) is
acquired by the flip's write into that partial unique index; a second live
publisher for the same real account raises `unique_violation`, handled as a
defer identical to a cap denial.

Outcome of the returned `(debited, flipped)` tuple, `02` §4 verbatim:
- `(1, 1)` → PROCEED (the caller commits and publishes).
- `(0, 0)` → DEFERRED — cap denied; the statement wrote nothing, commit is a
  no-op; the worker reschedules the job and the intent stays `approved`.
- `(1, 0)` → the intent was not `approved` (a race moved it): RAISE, so the
  caller's transaction rolls back and the debit rolls back with it. This is
  the leak the CTE coupling + asserted row count closes.
- `unique_violation` on `uq_publish_exclusive` → DEFERRED (real account already
  publishing/ambiguous somewhere); rolls the debit back, no error surfaced.

`cap_at_write` freezes the cap at the day's first debit — refund integrity: a
mid-day cap change must not make a later refund arithmetic-inconsistent
(`04` §L.2, `02` §6). The refund always targets the RECORDED debit day
(`cap_consumed_on`), so a tz change or midnight crossing between debit and
refund cannot touch the wrong bucket.

## Only the cadence spends the cap

The cap is the cadence's own (`daily_post_counts`, "OUR product cadence cap —
never Meta's", `055`). A story a person planned for a time of their choosing
(`origin = 'planned'`, 088) sits outside it (content schedule F8 (a)): it
neither spends the day nor waits on a spent one, so the day's cadence is what
it would have been without it. One predicate says which stories spend the cap,
:data:`_SPENDS_CAP_SQL`, and every write to a bucket asks it: the flip's debit,
the manual post's (:func:`flip_to_posted_by_hand`) and the one decrement both
refund legs share (:data:`_RETURN_DEBIT`). Those writes live in this module,
because a debit and a refund keyed on different answers would drift the day's
count. `tests/src/services/target/test_publish_cap.py` fails on a new SQL string
literal under `src/` that writes the table by its bare name; it does not see ORM
or Core writes, a built or schema-qualified name, or a write outside `src/`. A
story outside the cap still carries its day in `cap_consumed_on`, stamped
without a debit, since `publishing` and a manual `posted` require the column
(`ck_publishing_debited`, `ck_posted_complete`): the column says the story was
admitted on that day, not that a bucket was touched. Key 4, the cancel honour
and Meta's own limit hold it like any other story.
"""

from __future__ import annotations

import enum
from datetime import date

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from src.exceptions.base import StorydumpError
from src.services.target._dbapi import driver_candidates

#: key 4 — the partial unique index whose violation means "this real account is
#: already publishing/ambiguous in some workspace"; matched by name so an
#: unrelated unique violation is never swallowed as a defer.
_PUBLISH_EXCLUSIVE = "uq_publish_exclusive"

#: Which stories spend the account's daily cap (module docstring). Unaliased:
#: splice it only where `post_intents` is the one table in scope.
_SPENDS_CAP_SQL = "origin = 'cadence'"

#: The one decrement both refund legs share (module docstring): the recorded
#: day's bucket, and none for a story that never took one.
_RETURN_DEBIT = (
    "UPDATE daily_post_counts SET count = count - 1"
    " WHERE (workspace_id, ig_account_id, local_date) ="
    "       (:ws, :acct, (SELECT cap_consumed_on FROM post_intents"
    "                      WHERE id = :intent AND " + _SPENDS_CAP_SQL + "))"
    "   AND count > 0"
)


class FlipOutcome(enum.Enum):
    """The §4 flip's non-error outcomes. `(1,0)` is not here — it raises.

    `DEFERRED` is the cap's answer (the day is spent — wait for the next
    slot); `BUSY` is key 4's (a sibling of the real account is publishing
    right now — wait seconds, not a slot). The two were one value until
    2026-09-12, and a burst of approvals waited a day for its second story."""

    PROCEED = "proceed"
    DEFERRED = "deferred"
    BUSY = "busy"
    #: A cancel landed on the approved row (the float, plan 03 D3): the flip
    #: refused it in its own WHERE, so a cancel between the pipeline's read
    #: and its flip can never post after the card said cancelled.
    CANCELLED = "cancelled"


class IntentNotApproved(StorydumpError):
    """The flip debited but did not flip — the intent was not `approved`
    (a race terminalized or moved it). Raised so the caller's transaction,
    and the debit inside it, roll back (`02` §4 `(1,0)`)."""


def _is_publish_exclusive_violation(exc: BaseException) -> bool:
    for c in driver_candidates(exc):
        if getattr(c, "constraint_name", None) == _PUBLISH_EXCLUSIVE:
            return True
    return False


async def flip_to_publishing(
    session,
    *,
    intent_id: str,
    workspace_id: str,
    ig_account_id: str,
    local_date: date,
    effective_cap: int,
) -> FlipOutcome:
    """The `approved → publishing` flip (`02` §4), inside the caller's UoW tx.

    Returns PROCEED, DEFERRED (the cap), BUSY (key 4) or CANCELLED; raises
    :class:`IntentNotApproved` when the row is not approved.
    Runs the one CTE and reads back `(debited, flipped, was_approved,
    cancelling)` — the row counts ARE the decision, because a rowcount here
    cannot be faked the way #883's could: a debited-but-not-flipped tuple is
    a real race, not a self-transition no-op, and it is exactly what must
    roll back.

    Re-entrant since the float (plan 03 D3): a story that stepped back to
    `approved` between attempts carries `cap_consumed_on`, and the debit CTE
    runs only when it does not — the debit it carries IS the debit, so
    `(0,1,1)` proceeds and the cap counts the story exactly once. `COALESCE`
    keeps `ck_publishing_debited` true on re-entry. The flip's own WHERE
    refuses a `cancel_requested` row, closing the window between the
    pipeline's read and its flip.

    A story outside the cap (:data:`_SPENDS_CAP_SQL`) takes *local_date*
    without a debit and answers `(0,1,1)`; with no debit guarding it, the
    flip's own WHERE is all that refuses its cancel.
    """
    try:
        # In a SAVEPOINT: key 4's refusal (`uq_publish_exclusive`, a sibling
        # of the real account already publishing) is an error the database
        # raises, and without the savepoint it aborts the caller's admission
        # transaction — the deferral's own writes then fail and the job burns
        # an attempt on the failure ladder (2026-09-12, five approvals in six
        # seconds). Rolled back to here, the deferral proceeds like a cap
        # denial.
        async with session.begin_nested():
            row = (
                await session.execute(
                    text(
                        "WITH me AS ("
                        "  SELECT cap_consumed_on, cancel_requested,"
                        "         " + _SPENDS_CAP_SQL + " AS spends_cap"
                        "    FROM post_intents"
                        "   WHERE id = :intent AND state = 'approved'"
                        "), debit AS ("
                        "  INSERT INTO daily_post_counts AS d"
                        "    (workspace_id, ig_account_id, local_date, count, cap_at_write)"
                        "  SELECT :ws, :acct, :local_date, 1, :cap FROM me"
                        "   WHERE me.cap_consumed_on IS NULL AND NOT me.cancel_requested"
                        "     AND me.spends_cap"
                        "  ON CONFLICT (workspace_id, ig_account_id, local_date)"
                        "    DO UPDATE SET count = d.count + 1 WHERE d.count < d.cap_at_write"
                        "  RETURNING local_date"
                        "), flip AS ("
                        "  UPDATE post_intents p"
                        "     SET state = 'publishing',"
                        "         cap_consumed_on = COALESCE(p.cap_consumed_on,"
                        "                                    CAST(:local_date AS date))"
                        "   WHERE p.id = :intent AND p.state = 'approved'"
                        "     AND NOT p.cancel_requested"
                        "     AND (p.cap_consumed_on IS NOT NULL"
                        "          OR EXISTS (SELECT 1 FROM debit)"
                        "          OR NOT (SELECT spends_cap FROM me))"
                        "  RETURNING id"
                        ") SELECT (SELECT count(*) FROM debit) AS debited,"
                        "         (SELECT count(*) FROM flip)  AS flipped,"
                        "         (SELECT count(*) FROM me) AS was_approved,"
                        "         (SELECT bool_or(cancel_requested) FROM me) AS cancelling"
                    ),
                    {
                        "ws": workspace_id,
                        "acct": ig_account_id,
                        "local_date": local_date,
                        "cap": effective_cap,
                        "intent": intent_id,
                    },
                )
            ).one()
    except IntegrityError as exc:
        if _is_publish_exclusive_violation(exc):
            # key 4: the real account is already publishing/ambiguous elsewhere.
            # The savepoint rolled the debit back; the caller waits seconds.
            return FlipOutcome.BUSY
        raise

    debited, flipped = int(row.debited), int(row.flipped)
    was_approved = int(row.was_approved)
    if flipped == 1 and debited in (0, 1):
        # (1,1): a fresh debit; (0,1): re-entry on the debit the story
        # carries, or a story outside the cap on its stamped day.
        return FlipOutcome.PROCEED
    if flipped == 0 and debited == 0:
        if was_approved == 0:
            # The row is not approved (publishing, terminal, gone): today's
            # (0,0) misread that as a cap denial and deferred a row that will
            # never flip; it is the caller's error to answer.
            raise IntentNotApproved(f"intent {intent_id}: no approved row to flip")
        if bool(row.cancelling):
            return FlipOutcome.CANCELLED
        return FlipOutcome.DEFERRED
    # (1, 0): debited but the intent did not flip. Roll it all back.
    raise IntentNotApproved(
        f"intent {intent_id}: cap debited but flip matched no approved row"
        f" (debited={debited}, flipped={flipped}) — rolling back (#862 §4)"
    )


async def flip_to_posted_by_hand(
    session,
    *,
    intent_id: str,
    workspace_id: str,
    ig_account_id: str,
    tz: str,
    effective_cap: int,
) -> bool:
    """`awaiting_approval → posted` by hand (`06` §3, the manual path's Posted
    tap) and the day's debit, in ONE statement. The debit reads the flip's
    row, so it lands only on a flipped story and in the bucket of the day the
    story is stamped with. For a story that spends the cap it is
    unconditional: the story is already on Instagram, so refusing to record it
    would misstate the day, the over-posting direction R1 exists to avoid, and
    `cap_at_write` freezes at the day's first debit as the flip's does. Either
    way the row carries its account-local day (`ck_posted_complete`). Returns
    False when the row was not awaiting approval."""
    row = (
        await session.execute(
            text(
                "WITH flip AS ("
                "  UPDATE post_intents"
                "     SET state = 'posted', published_via = 'manual',"
                "         cap_consumed_on = (now() AT TIME ZONE fn_safe_tz(:tz))::date"
                "   WHERE id = :intent AND state = 'awaiting_approval'"
                "  RETURNING cap_consumed_on, " + _SPENDS_CAP_SQL + " AS spends_cap"
                "), debit AS ("
                "  INSERT INTO daily_post_counts AS d"
                "    (workspace_id, ig_account_id, local_date, count, cap_at_write)"
                "  SELECT :ws, :acct, flip.cap_consumed_on, 1, :cap FROM flip"
                "   WHERE flip.spends_cap"
                "  ON CONFLICT (workspace_id, ig_account_id, local_date)"
                "    DO UPDATE SET count = d.count + 1"
                ") SELECT (SELECT count(*) FROM flip) AS flipped"
            ),
            {
                "ws": workspace_id,
                "acct": ig_account_id,
                "tz": tz,
                "cap": effective_cap,
                "intent": intent_id,
            },
        )
    ).one()
    return int(row.flipped) == 1


async def refund_cap(
    session, *, intent_id: str, workspace_id: str, ig_account_id: str
) -> None:
    """Return the debit recorded for *intent_id* and stamp `cap_refunded_at`.

    The companion of a terminal `publishing|publishing_ambiguous|review_required
    → failed` flip, in the SAME transaction as that flip. Targets the RECORDED
    day (`cap_consumed_on`), never `now()`'s day, so a midnight crossing cannot
    refund the wrong bucket; `AND count > 0` keeps `ck_dpc_nonneg`.
    """
    await session.execute(
        text(_RETURN_DEBIT),
        {"ws": workspace_id, "acct": ig_account_id, "intent": intent_id},
    )
    await session.execute(
        text("UPDATE post_intents SET cap_refunded_at = now() WHERE id = :intent"),
        {"intent": intent_id},
    )


async def resolve_retry(
    session,
    *,
    intent_id: str,
    workspace_id: str,
    ig_account_id: str,
    attempts_by_step: dict,
) -> bool:
    """`review_required → approved`, debit-neutral, generation bumped
    (pass-5, `02` §4). Returns False if a race resolved the intent first.

    Refunds the recorded day FIRST (the flip below NULLs the pointer it
    targets), then re-enters the working states as if freshly approved — so the
    NEXT flip re-debits the current day EXACTLY once. `ck_refund_after_debit`
    holds throughout (both columns end NULL) and `ck_publishing_debited`
    re-arms. The zero-row intent UPDATE rolls the refund back with it.
    """
    import json

    await session.execute(
        text(_RETURN_DEBIT),
        {"ws": workspace_id, "acct": ig_account_id, "intent": intent_id},
    )
    flipped = (
        await session.execute(
            text(
                "UPDATE post_intents"
                "   SET state = 'approved',"
                "       cap_consumed_on = NULL, cap_refunded_at = NULL,"
                "       publish_step = 'none', ig_container_id = NULL,"
                "       transit_asset_ref = NULL,"
                "       attempts_by_step = CAST(:attempts AS jsonb)"
                " WHERE id = :intent AND state = 'review_required'"
                " RETURNING id"
            ),
            {"attempts": json.dumps(attempts_by_step), "intent": intent_id},
        )
    ).fetchone()
    return flipped is not None


async def resolve_posted(session, *, intent_id: str) -> bool:
    """`review_required → posted` — "it did publish" (`02` §4 resolve-posted,
    pass 2): legal only when a publish call was made (`publish_step =
    'publish_called'`, the container present) — the WHERE is the rule, so a
    caller cannot confirm a post Instagram was never asked for. The one
    statement sets `publish_step = 'effect_confirmed'` and `published_via =
    'api'` with the flip, satisfying `ck_posted_complete` (the debit, taken
    at the flip to `publishing`, stands). No media id is known; the
    reconciler's evidence, if any, is on `last_error`. Returns False on a
    lost race or an intent that never called publish."""
    flipped = (
        await session.execute(
            text(
                "UPDATE post_intents"
                "   SET state = 'posted', publish_step = 'effect_confirmed',"
                "       published_via = 'api'"
                " WHERE id = :intent AND state = 'review_required'"
                "   AND publish_step = 'publish_called'"
                "   AND ig_container_id IS NOT NULL"
                "   AND cap_consumed_on IS NOT NULL"
                " RETURNING id"
            ),
            {"intent": intent_id},
        )
    ).fetchone()
    return flipped is not None


async def resolve_cancel(session, *, intent_id: str) -> bool:
    """`review_required → cancelled`, RETAINING the debit (pass-5 decision):
    a `review_required` intent may have published, so refunding on cancel risks
    under-counting toward over-posting. Returns False on a lost race.

    Debit retention is by OMISSION — a plain state flip that touches neither
    `daily_post_counts` nor the cap columns; the transition trigger validates
    the edge. The operator who has determined it did NOT publish uses
    resolve-failed instead (which refunds).
    """
    flipped = (
        await session.execute(
            text(
                "UPDATE post_intents SET state = 'cancelled'"
                " WHERE id = :intent AND state = 'review_required'"
                " RETURNING id"
            ),
            {"intent": intent_id},
        )
    ).fetchone()
    return flipped is not None
