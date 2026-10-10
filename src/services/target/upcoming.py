"""What is coming on the calendar: for a range of the workspace's local days,
the planned stories still scheduled and the slots the cadence will open.

## Predicted slots are projected, never read

A cadence story exists only once its slot comes due. The clock mints a
`plan_slot` job when an account's cursor (`ig_accounts.next_slot_at`) is due,
and advances the cursor with `fn_next_slot` in the same statement
(`.claude/rules/scheduler.md`), so the days ahead hold no cadence rows to read.

:func:`upcoming` answers with the clock's own function. It walks an account's
slots with `fn_next_slot`, one step per slot, under the settings the clock
advances the cursor by: the account's own time zone, posts per day and posting
hours, and the workspace's where an override is NULL. Nothing here restates
the spacing, so a day the clocks change comes out as the clock will mint it.
An account the clock does not post for (`content_runway._POSTING_SQL`) is not
walked.

## Where a walk starts

At the cursor, as the clock's does, when the cursor is no more than
:data:`WALK_LEAD_DAYS` before the range, or before now once the range has
begun. A cursor set under other settings stands off today's grid, and the
clock mints it as it stands.

Otherwise ahead of the cursor: at the slot `fn_next_slot` answers for the
instant that far before the range. That is the path for any month after the
current one, and for a cursor that has fallen behind. The function answers any
instant with a slot of the same daily grid, a grid the settings alone fix, so
the two walks meet before the range begins, and a walk costs what its range
holds wherever the cursor is.

## A projection is not a story

Every such row says ``kind: predicted`` and carries no state. It is a place
the cadence will look for a story, not a promise of one: `plan_slot` mints
nothing for a slot with no eligible media. A slot whose time has come is left
out, because the clock is minting it, or has.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from src.services.target import readers, vocabulary
from src.services.target.content_runway import _POSTING_SQL, _POSTS_PER_DAY_SQL
from src.services.target.workspaces import (
    _INTENT_COLUMNS,
    _INTENT_FROM,
    LOCAL_DAY_SQL,
    LOCAL_MIDNIGHT_SQL,
)

#: How long before the range a walk starts when it does not start at the
#: cursor. It needs a lead at all because `fn_next_slot` answers with a slot
#: after the instant it is given, never one at it, and may pass over the first
#: one after: a walk begun at the range itself would lose the range's first
#: slots. Two days, because the function answers within a local day of that
#: instant, 25 hours when the clocks go back, and two walks a clock change set
#: apart are one again within hours of it.
WALK_LEAD_DAYS = 2

#: The most planned stories one read returns.
PLANNED_MAX = 500

#: The most predicted slots one read returns: the widest range for accounts
#: that post a hundred times a day between them.
PREDICTED_MAX = vocabulary.RANGE_MAX_DAYS * 100

#: The range's two bounds as instants, as the intents read bounds a slot.
_RANGE_START = LOCAL_MIDNIGHT_SQL.format(name="from_date")
_RANGE_END = LOCAL_MIDNIGHT_SQL.format(name="to_date")

#: The intents read's row and its day. The row's own column list, so what the
#: queue's row gains the calendar's gains.
_PLANNED_SQL = (
    f"SELECT {_INTENT_COLUMNS},"
    f" {LOCAL_DAY_SQL.format(slot='i.schedule_slot_at', tz='w.tz')}"
    f"{_INTENT_FROM}"
    " WHERE i.workspace_id = :ws AND i.origin = 'planned' AND i.state = 'scheduled'"
    f"   AND i.schedule_slot_at >= {_RANGE_START}"
    f"   AND i.schedule_slot_at < {_RANGE_END}"
    " ORDER BY i.schedule_slot_at, i.id LIMIT :lim"
)

#: The slot after *after*, as the clock's `plan_slot` leg advances a cursor
#: (`fn_clock_tick`): its function, over its settings, in its order.
_NEXT_SLOT = "fn_next_slot({after}, a.eff_tz, a.eff_start, a.eff_end, a.eff_ppd)"

#: `acct` is the accounts the clock posts for, each with the settings that leg
#: advances its cursor by, under the leg's own names: the account's, else the
#: workspace's. Each step of `slots` is that advance. A step is taken from
#: every slot before the range's end, so an account's last one lands past it,
#: and the outer filter drops that. `test_upcoming.py` pins the accounts, the
#: settings and the step to the migration.
_PREDICTED_SQL = (
    "WITH RECURSIVE acct AS ("
    "  SELECT a.id, a.handle, a.display_name, a.next_slot_at, w.tz AS ws_tz,"
    "         COALESCE(a.tz, w.tz) AS eff_tz,"
    f"         {_POSTS_PER_DAY_SQL} AS eff_ppd,"
    "         COALESCE(a.posting_hours_start, w.posting_hours_start) AS eff_start,"
    "         COALESCE(a.posting_hours_end, w.posting_hours_end) AS eff_end"
    "    FROM ig_accounts a JOIN workspaces w ON w.id = a.workspace_id"
    f"   WHERE a.workspace_id = :ws AND w.id = :ws AND {_POSTING_SQL}"
    "), bounds AS ("
    "  SELECT r.lo, r.hi,"
    "         GREATEST(r.lo, now()) - make_interval(days => :lead) AS walk_from"
    f"    FROM (SELECT {_RANGE_START} AS lo, {_RANGE_END} AS hi) r"
    "), slots (id, slot, step) AS ("
    "  SELECT a.id,"
    "         CASE WHEN a.next_slot_at >= b.walk_from THEN a.next_slot_at"
    f"              ELSE {_NEXT_SLOT.format(after='b.walk_from')} END,"
    "         1"
    "    FROM acct a CROSS JOIN bounds b"
    "  UNION ALL"
    f"  SELECT s.id, {_NEXT_SLOT.format(after='s.slot')}, s.step + 1"
    "    FROM slots s JOIN acct a ON a.id = s.id CROSS JOIN bounds b"
    "   WHERE s.slot < b.hi AND s.step <= a.eff_ppd * :span_days"
    ")"
    " SELECT 'predicted' AS kind, s.slot AS schedule_slot_at,"
    f"        {LOCAL_DAY_SQL.format(slot='s.slot', tz='a.ws_tz')}, a.eff_tz AS tz,"
    "        a.id AS ig_account_id, a.handle AS account_handle,"
    "        a.display_name AS account_display_name"
    "   FROM slots s JOIN acct a ON a.id = s.id CROSS JOIN bounds b"
    "  WHERE s.slot >= b.lo AND s.slot > now() AND s.slot < b.hi"
    "  ORDER BY s.slot, a.id LIMIT :lim"
)


async def upcoming(
    executor,
    *,
    workspace_id: str,
    from_date: date,
    to_date: date,
    planned_limit: int = PLANNED_MAX,
    predicted_limit: int = PREDICTED_MAX,
) -> dict[str, Any]:
    """The planned stories and the predicted slots of the workspace's local
    days ``[from_date, to_date)``, each list soonest first.

    **The range** is local days in the WORKSPACE's zone, at most
    `vocabulary.RANGE_MAX_DAYS` of them: a wider one is cut to that.

    **``planned``** holds the stories a person scheduled that are still
    `scheduled`. A row is the intents read's row (`workspaces._INTENT_COLUMNS`)
    and its ``day``, so the page draws it as it draws a queue row.

    **``predicted``** holds the slots the cadence will open (the module
    docstring has how). A row carries ``kind`` and the keys the two kinds
    share: ``schedule_slot_at``, ``day``, ``tz``, and the account by id, handle
    and display name.

    A row's ``day`` is its slot's date in the workspace's zone, and its ``tz``
    the zone its time reads in: the account's, else the workspace's.

    **Bounds.** ``planned_truncated`` and ``predicted_truncated`` say a list
    was cut at its limit. A cut list is whole up to its last row's ``day``,
    which may itself be short. The limits bound the answer, not the work, which
    is one walk for each account the clock posts for; a walk takes no more
    steps than the days it spans hold slots, which cuts no walk and stops one
    that no longer advances."""
    # No wider than the caller's own span, so the sum cannot pass the last date.
    days = min((to_date - from_date).days, vocabulary.RANGE_MAX_DAYS)
    bounds = {
        "ws": str(workspace_id),
        "from_date": from_date,
        "to_date": from_date + timedelta(days=days),
    }
    planned = await readers.rows(
        executor, _PLANNED_SQL, lim=int(planned_limit) + 1, **bounds
    )
    predicted = await readers.rows(
        executor,
        _PREDICTED_SQL,
        lead=WALK_LEAD_DAYS,
        # A walk spans the range and the lead, with a part of a day at each
        # end, and a local day holds at most the account's posts per day.
        span_days=days + WALK_LEAD_DAYS + 2,
        lim=int(predicted_limit) + 1,
        **bounds,
    )
    return {
        "planned": planned[:planned_limit],
        "planned_truncated": len(planned) > planned_limit,
        "predicted": predicted[:predicted_limit],
        "predicted_truncated": len(predicted) > predicted_limit,
    }
