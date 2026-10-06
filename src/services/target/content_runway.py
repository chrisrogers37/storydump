"""Days of content left per account, and the one notice when it runs low (#1478).

A workspace hears when its library is already EMPTY (`scheduler._notice_no_media`,
on the slot that found nothing) and when a Drive folder or an Instagram
connection dies. Nothing told it before the library ran dry. This module is
that warning, and the figure behind it.

## One definition of "eligible"

The runway counts the pool the planner draws from, so the two cannot disagree:
`category_mix.pool` IS the planner's read (`scheduler.execute_plan_slot` draws
from it), and the count is the eligible files in the folders it can draw from.
Eligible means `available`, not already live for this account, and under no
live lock — a workspace-wide skip or reject, or this account's own `recent`
lock. Drawable means a connected folder that is not in `error` and not Off
(`category_mix.weights`). Nothing else counts, because nothing else can post.

## What the figure means

Days left is the eligible files over the account's posts per day — its
override, else the workspace's (`fn_next_slot`'s cadence) — in whole days,
rounded down: :func:`days_left`, the one computation the Overview shows, the
notice names and the latch compares. It is a floor, not a forecast: a posted
file's `recent` lock expires after `repost_ttl_days` and puts the file back,
which this does not count ahead of time. It is None for an account the clock
does not post for (the account or its workspace inactive, or the workspace
paused), because a runway that is not being spent has no end.

## Once per crossing

`execute_plan_slot` asks :func:`after_mint` what a mint owes the workspace.
Below :data:`LOW_RUNWAY_DAYS` it is told once; the next notice waits until the
account has climbed back :data:`REARM_MARGIN_DAYS` above that level. The gap is
deliberate: files whose `recent` lock expires come back one at a time while
the account keeps posting, so an account near the line would otherwise cross
it every few slots and be told each time.

The latch is the audit trail, not a column (the issue asked for no DDL): the
notice writes a `low_content_notice` row on the account, a re-arm a
`low_content_rearmed` row, and the newest of the two is the state. It is read
and written under the account's own advisory lock, inside `plan_slot`, which
the job layer already serializes per account (`acct:<id>`), so one writer
decides at a time. The latch lives in `audit_events`, so a retention sweep
that deletes an account's latch row re-arms it, and the next crossing below
the level is told again.

## The notice never decides the slot

The mint is the slot's work; the notice is said beside it. `execute_plan_slot`
settles the latch in a savepoint after the mint and returns the minted intent
alone, so the slot's job finalizes succeeded whether or not anyone could be
told, and a failure writing the notice is logged and costs the mint nothing.
A workspace with no push binding is latched all the same (the row's `told` is
0) with a warning in the log, once per crossing; the Overview's card is that
workspace's channel. Only the empty library's notice, where nothing was
minted, can park a `plan_slot` job for review.
"""

from __future__ import annotations

import logging
from typing import Optional, Union

from sqlalchemy import text

from src.services.target import (
    audit,
    category_mix,
    outbox,
    prompts,
    readers,
    workspaces,
)

logger = logging.getLogger(__name__)

#: Fewer days of eligible content than this, and the workspace is told. A week:
#: the one early notice has to leave time to shoot or collect new media, add it
#: to Drive and let the next sync pick it up, and a week is one content-planning
#: cycle. It is also the level the legacy pool-health check warned at (#152).
LOW_RUNWAY_DAYS = 7

#: How far above the warning level an account must climb before a later drop
#: is told again: one day's posts, so a single file coming back off its
#: `recent` lock cannot re-arm the notice. A margin over the level rather than
#: a level of its own, so the re-arm moves with the warning level and can never
#: sit at or below it.
REARM_MARGIN_DAYS = 1

#: The two audit events the latch is made of (`entity_kind = 'ig_account'`).
NOTICE_EVENT = "low_content_notice"
REARM_EVENT = "low_content_rearmed"

#: Whether the clock posts for an account: fn_clock_tick's own `plan_slot`
#: predicate (084, leg 2) less its due-now conjunct, read here because nothing
#: serves it per account. `test_content_runway.py` pins it to the migration.
_POSTING_SQL = (
    "(a.state = 'active' AND a.next_slot_at IS NOT NULL"
    " AND w.state = 'active' AND NOT w.is_paused)"
)

#: The posts per day an account is spent at: its own, else its workspace's —
#: the cadence the same leg advances the account's slot cursor by (its
#: `eff_ppd`). `test_content_runway.py` pins it to the migration too.
_POSTS_PER_DAY_SQL = "COALESCE(a.posts_per_day, w.posts_per_day)"


def days_left(eligible: int, posts_per_day: Optional[int]) -> Optional[int]:
    """Whole days of eligible content at *posts_per_day*, rounded down, so a
    part of a day is never counted as one. The one place the figure is
    computed: the Overview shows it, the notice names it and the latch
    compares it (:func:`latch_action`).

    None with no posts per day to divide by. The database's CHECKs hold every
    cadence between 1 and 50, so the guard only keeps a division by zero out:
    an account that posts nothing is one the clock does not post for, which
    :func:`runway` reads as ``posting``."""
    if not posts_per_day or posts_per_day <= 0:
        return None
    return eligible // posts_per_day


def is_low(days: Optional[int], below_days: int) -> bool:
    """Whether *days* of content left (:func:`days_left`) is under the
    warning level *below_days*. The one comparison: the notice is owed by it
    (:func:`latch_action`) and the Overview marks an account ``low`` by it
    (:func:`runway`), so the card marks exactly the accounts the notice is
    about. Never low with no days, which is an account not being posted for.
    """
    return days is not None and days < below_days


def latch_action(
    *,
    days: Optional[int],
    latched: bool,
    below_days: int,
) -> Optional[str]:
    """What one mint owes the latch at *days* of content left
    (:func:`days_left`): tell (`NOTICE_EVENT`), re-arm (`REARM_EVENT`), or
    nothing (None).

    Told once on the way down, below *below_days* (:func:`is_low`); re-armed
    only at :data:`REARM_MARGIN_DAYS` above it or more, so the band between
    the two changes nothing in either direction. The days are whole, so fewer
    than *below_days* of them is exactly fewer files than *below_days* days of
    posts. Nothing with no days, the case the database's CHECKs rule out."""
    if days is None:
        return None
    if is_low(days, below_days):
        return None if latched else NOTICE_EVENT
    if latched and days >= below_days + REARM_MARGIN_DAYS:
        return REARM_EVENT
    return None


def notice_text(*, label: str, days: int, eligible: int, posts_per_day: int) -> str:
    """The notice, naming the account, its *days* left — the figure the
    Overview shows (:func:`days_left`) — and the arithmetic behind them.

    No eligible file at all is said as no content, never as "less than a
    day": that span is the part of a day a few files still cover."""
    if not eligible:
        return (
            f"⏳ {label} has no content left. Add media to this workspace's"
            " Drive source to keep it posting."
        )
    if not days:
        span = "less than a day"
    elif days == 1:
        span = "about 1 day"
    else:
        span = f"about {days} days"
    files = "1 file" if eligible == 1 else f"{eligible} files"
    return (
        f"⏳ {label} has {span} of content left ({files} at"
        f" {posts_per_day} a day). Add media to this workspace's Drive source"
        " to keep it posting."
    )


async def after_mint(
    session,
    *,
    workspace_id: str,
    ig_account_id: str,
    eligible: int,
    below_days: int,
) -> Union[int, str, None]:
    """Settle the runway latch after a mint left *eligible* files.

    Returns the number of outbox rows written when the workspace was told,
    `outbox.UNDELIVERABLE` when it was owed the notice and had no push binding
    to receive it, and None when nothing was owed.

    The latch is read, decided and written under the account's own advisory
    lock. The job layer's `acct:<id>` lease already runs one `plan_slot` per
    account at a time; the lock makes the read-then-write one decision in the
    database itself, rather than resting on the lease alone.

    **The latch row is written on the undeliverable attempt too**, as
    `_notice_no_media` stamps its marker: it records that this crossing was
    spoken for, so a workspace with no binding is warned about once per
    crossing in the log rather than once per slot. The return value is for
    the caller to read, not to finalize on: `execute_plan_slot` minted
    before it asked, so its job succeeds either way (module docstring, "The
    notice never decides the slot").
    """
    # Its own statement, before the read: a second transaction on the account
    # waits here until the first ends, and the read below, a statement of its
    # own, then sees what the first wrote. The lock dies with the transaction.
    # 64-bit (`hashtextextended`), as every namespace keyed on a tenant's rows
    # is (#1370, `test_advisory_lock_widths.py`): a collision can only
    # over-serialize, and the wide hash makes one rarer.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"runway:{ig_account_id}"},
    )
    account = await readers.row(
        session,
        "SELECT a.handle,"
        f"      {_POSTS_PER_DAY_SQL} AS posts_per_day,"
        "       (SELECT e.detail->>'event' FROM audit_events e"
        "         WHERE e.workspace_id = a.workspace_id"
        "           AND e.entity_kind = 'ig_account' AND e.entity_id = a.id"
        "           AND e.detail->>'event' IN (:told, :rearmed)"
        "         ORDER BY e.id DESC LIMIT 1) AS latch"
        "  FROM ig_accounts a JOIN workspaces w ON w.id = a.workspace_id"
        " WHERE a.id = CAST(:acct AS uuid) AND a.workspace_id = :ws",
        acct=str(ig_account_id),
        ws=str(workspace_id),
        told=NOTICE_EVENT,
        rearmed=REARM_EVENT,
    )
    if account is None:
        # The job's own account, read in the job's transaction: not reachable
        # in practice, and nothing can be said about an account not there.
        logger.warning(
            "plan_slot: account %s not readable for its runway — nothing said",
            ig_account_id,
        )
        return None
    posts_per_day = account["posts_per_day"]
    days = days_left(eligible, posts_per_day)
    action = latch_action(
        days=days,
        latched=account["latch"] == NOTICE_EVENT,
        below_days=below_days,
    )
    if action is None:
        return None

    detail = {
        "v": 1,
        "event": action,
        "eligible": eligible,
        "posts_per_day": posts_per_day,
    }
    if action == REARM_EVENT:
        detail["rearm_days"] = below_days + REARM_MARGIN_DAYS
        await _latch(session, workspace_id, ig_account_id, detail)
        logger.info(
            "plan_slot: account %s is back to %d file(s) — runway notice re-armed",
            ig_account_id,
            eligible,
        )
        return None

    bindings = await prompts.push_bindings(session, workspace_id)
    detail.update(below_days=below_days, told=len(bindings))
    await _latch(session, workspace_id, ig_account_id, detail)
    if not bindings:
        logger.warning(
            "plan_slot: account %s has %d file(s) left at %d a day and its"
            " workspace has NO push binding — nobody was told (#1090 D5)",
            ig_account_id,
            eligible,
            posts_per_day,
        )
        return outbox.UNDELIVERABLE
    await outbox.fanout_notification(
        session,
        workspace_id=workspace_id,
        bindings=bindings,
        text=notice_text(
            label=prompts.account_label(account["handle"]),
            days=days,
            eligible=eligible,
            posts_per_day=posts_per_day,
        ),
    )
    logger.info(
        "plan_slot: account %s has %d file(s) left at %d a day — notified"
        " %d binding(s)",
        ig_account_id,
        eligible,
        posts_per_day,
        len(bindings),
    )
    return len(bindings)


async def _latch(session, workspace_id: str, ig_account_id: str, detail: dict):
    await audit.record(
        session,
        workspace_id=str(workspace_id),
        entity_kind="ig_account",
        entity_id_sql="CAST(:acct AS uuid)",
        # `plan_slot` runs for an active account only (the adapter's check).
        from_state="active",
        to_state="active",
        detail=detail,
        acct=str(ig_account_id),
    )


async def runway(executor, *, workspace_id: str, below_days: int) -> dict:
    """The Overview's read: per account, the eligible files, the posts per day
    they are spent at, and the whole days left (#1478).

    Every destination the Accounts tab lists, in the same order
    (`workspaces.LISTED_ACCOUNT_SQL`). ``low`` is the notice's own test
    (:func:`is_low`) at *below_days*, which the caller passes from the
    worker's level (`WorkerConfig.low_runway_days`), so the card marks
    exactly the accounts the notice is about. The folders are read once, for
    every account."""
    accounts = await readers.rows(
        executor,
        "SELECT a.id, a.handle, a.display_name, a.state,"
        f"      {_POSTS_PER_DAY_SQL} AS posts_per_day,"
        f"      {_POSTING_SQL} AS posting"
        "  FROM ig_accounts a JOIN workspaces w ON w.id = a.workspace_id"
        " WHERE a.workspace_id = :ws"
        f"   AND {workspaces.LISTED_ACCOUNT_SQL}"
        f" {workspaces.LISTED_ACCOUNT_ORDER_SQL}",
        ws=str(workspace_id),
    )
    folders = await category_mix.pool_folders(executor, workspace_id=str(workspace_id))
    out = []
    for account in accounts:
        drawn = await category_mix.pool(
            executor,
            workspace_id=str(workspace_id),
            ig_account_id=str(account["id"]),
            folders=folders,
        )
        posting = bool(account["posting"])
        posts_per_day = int(account["posts_per_day"])
        eligible = drawn.eligible
        days = days_left(eligible, posts_per_day) if posting else None
        out.append(
            {
                "id": str(account["id"]),
                "handle": account["handle"],
                "display_name": account["display_name"],
                "state": account["state"],
                "posting": posting,
                "posts_per_day": posts_per_day,
                "eligible": eligible,
                "days_left": days,
                "low": is_low(days, below_days),
            }
        )
    return {"below_days": below_days, "accounts": out}
