"""L.5 — the §4 cap ledger: the atomic flip, the refund, and the two resolve
edges (#862, `02` §4).

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
"""

from __future__ import annotations

import enum
from datetime import date
from typing import Optional

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from src.exceptions.base import StorydumpError
from src.services.target._dbapi import driver_candidates

#: key 4 — the partial unique index whose violation means "this real account is
#: already publishing/ambiguous in some workspace"; matched by name so an
#: unrelated unique violation is never swallowed as a defer.
_PUBLISH_EXCLUSIVE = "uq_publish_exclusive"


class FlipOutcome(enum.Enum):
    """The §4 flip's two non-error outcomes. `(1,0)` is not here — it raises."""

    PROCEED = "proceed"
    DEFERRED = "deferred"


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

    Returns PROCEED or DEFERRED; raises :class:`IntentNotApproved` on `(1,0)`.
    Runs the one CTE and reads back `(debited, flipped)` — the row counts ARE
    the decision, because a rowcount here cannot be faked the way #883's could:
    a debited-but-not-flipped tuple is a real race, not a self-transition
    no-op, and it is exactly what must roll back.
    """
    try:
        row = (
            await session.execute(
                text(
                    "WITH debit AS ("
                    "  INSERT INTO daily_post_counts AS d"
                    "    (workspace_id, ig_account_id, local_date, count, cap_at_write)"
                    "  VALUES (:ws, :acct, :local_date, 1, :cap)"
                    "  ON CONFLICT (workspace_id, ig_account_id, local_date)"
                    "    DO UPDATE SET count = d.count + 1 WHERE d.count < d.cap_at_write"
                    "  RETURNING local_date"
                    "), flip AS ("
                    "  UPDATE post_intents"
                    "     SET state = 'publishing',"
                    "         cap_consumed_on = (SELECT local_date FROM debit)"
                    "   WHERE id = :intent AND state = 'approved'"
                    "     AND EXISTS (SELECT 1 FROM debit)"
                    "  RETURNING id"
                    ") SELECT (SELECT count(*) FROM debit) AS debited,"
                    "         (SELECT count(*) FROM flip)  AS flipped"
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
            # Defer exactly like a cap denial — the caller rolls back the debit.
            return FlipOutcome.DEFERRED
        raise

    debited, flipped = int(row.debited), int(row.flipped)
    if debited == 1 and flipped == 1:
        return FlipOutcome.PROCEED
    if debited == 0 and flipped == 0:
        return FlipOutcome.DEFERRED
    # (1, 0): debited but the intent was not 'approved'. Roll it all back.
    raise IntentNotApproved(
        f"intent {intent_id}: cap debited but flip matched no approved row"
        f" (debited={debited}, flipped={flipped}) — rolling back (#862 §4)"
    )


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
        text(
            "UPDATE daily_post_counts SET count = count - 1"
            " WHERE (workspace_id, ig_account_id, local_date) ="
            "       (:ws, :acct, (SELECT cap_consumed_on FROM post_intents"
            "                      WHERE id = :intent))"
            "   AND count > 0"
        ),
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
        text(
            "UPDATE daily_post_counts SET count = count - 1"
            " WHERE (workspace_id, ig_account_id, local_date) ="
            "       (:ws, :acct, (SELECT cap_consumed_on FROM post_intents"
            "                      WHERE id = :intent))"
            "   AND count > 0"
        ),
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


async def current_day_debit(
    session, *, workspace_id: str, ig_account_id: str, local_date: date
) -> Optional[int]:
    """The recorded `count` for a (workspace, account, day), or None. Read
    door for tests and the usage pre-check — never a pre-validation of a flip
    (the flip's own atomic denial is the authority)."""
    row = (
        await session.execute(
            text(
                "SELECT count FROM daily_post_counts"
                " WHERE workspace_id = :ws AND ig_account_id = :acct"
                "   AND local_date = :local_date"
            ),
            {"ws": workspace_id, "acct": ig_account_id, "local_date": local_date},
        )
    ).fetchone()
    return int(row[0]) if row else None
