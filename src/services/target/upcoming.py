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

The walk starts at the cursor, as the clock's does. A cursor more than
:data:`WALK_LEAD_DAYS` before the range is not walked from: the walk starts at
the slot `fn_next_slot` answers for the instant that far before the range.
The function answers any instant with a slot of the same daily grid, so the
two walks meet before the range begins, and a read costs what its range holds
however far behind a cursor is.

A projection is not a story. Every such row says ``kind: predicted`` and
carries no state. It is a place the cadence will look for a story, not a
promise of one: `plan_slot` mints nothing for a slot with no eligible media. A
slot whose time has come is left out, because the clock is minting it, or has.

An account the clock does not post for has no predicted slot
(`content_runway._POSTING_SQL`): one that is not `active` or has no cursor,
and any account of a workspace that is not `active` or is paused.

## The range, and what a row carries

``[from_date, to_date)`` are local days in the WORKSPACE's zone, at most
`vocabulary.RANGE_MAX_DAYS` of them. Every row's ``day`` is its slot's date in that
zone, and its ``tz`` the zone its time reads in: the account's, else the
workspace's.

A planned row is the intents read's row (`workspaces._INTENT_COLUMNS`) and its
``day``, so the page draws it as it draws a queue row. A predicted row carries
the keys the two share: ``schedule_slot_at``, ``day``, ``tz``, and the account
by id, handle and display name.

## Bounds

Each list has its own limit and says when it was cut. A walk takes no more
steps than the days it spans hold slots, a bound that cuts no walk and stops
one that no longer advances.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from src.services.target import readers, vocabulary
from src.services.target.content_runway import _POSTING_SQL, _POSTS_PER_DAY_SQL
from src.services.target.workspaces import (
    _INTENT_COLUMNS,
    _INTENT_FROM,
    _LOCAL_MIDNIGHT,
)

#: How long before the range a walk may start when the cursor is further back.
#: `fn_next_slot` answers within a local day of the instant it is given, 25
#: hours when the clocks go back, and two walks a clock change set apart are
#: one again within hours of it. Two days holds both.
WALK_LEAD_DAYS = 2

#: The most planned stories one read returns.
PLANNED_MAX = 500

#: The most predicted slots one read returns: the widest range for accounts
#: that post a hundred times a day between them.
PREDICTED_MAX = vocabulary.RANGE_MAX_DAYS * 100

#: The range's two bounds as instants: midnight of each date in the workspace's
#: own zone, as the intents read bounds a slot.
_RANGE_START = _LOCAL_MIDNIGHT.format(name="from_date")
_RANGE_END = _LOCAL_MIDNIGHT.format(name="to_date")

#: The calendar's day for a slot: its date in the workspace's zone.
_LOCAL_DAY = "to_char({slot} AT TIME ZONE {tz}, 'YYYY-MM-DD') AS day"

_PLANNED_SQL = (
    f"SELECT {_INTENT_COLUMNS},"
    f" {_LOCAL_DAY.format(slot='i.schedule_slot_at', tz='w.tz')}"
    f"{_INTENT_FROM}"
    " WHERE i.workspace_id = :ws AND i.origin = 'planned' AND i.state = 'scheduled'"
    f"   AND i.schedule_slot_at >= {_RANGE_START}"
    f"   AND i.schedule_slot_at < {_RANGE_END}"
    " ORDER BY i.schedule_slot_at, i.id LIMIT :lim"
)

#: The settings the clock advances an account's cursor by, under the names its
#: `plan_slot` leg selects them by (`fn_clock_tick`): the account's own, else
#: the workspace's. `test_upcoming.py` pins each to the migration.
_EFFECTIVE_SQL = {
    "eff_tz": "COALESCE(a.tz, w.tz)",
    "eff_ppd": _POSTS_PER_DAY_SQL,
    "eff_start": "COALESCE(a.posting_hours_start, w.posting_hours_start)",
    "eff_end": "COALESCE(a.posting_hours_end, w.posting_hours_end)",
}

#: The slot after *after*, as the same leg advances a cursor: the arguments in
#: its order, which `test_upcoming.py` pins to the migration too.
_NEXT_SLOT = "fn_next_slot({after}, a.eff_tz, a.eff_start, a.eff_end, a.eff_ppd)"

#: `acct` is the accounts the clock posts for, and each step of `slots` is the
#: clock's cursor advance. A step is taken from every slot before the range's
#: end, so an account's last one lands past it, and the outer filter drops that.
_PREDICTED_SQL = (
    "WITH RECURSIVE acct AS ("
    "  SELECT a.id, a.handle, a.display_name, a.next_slot_at, w.tz AS ws_tz, "
    + ", ".join(f"{sql} AS {name}" for name, sql in _EFFECTIVE_SQL.items())
    + "    FROM ig_accounts a JOIN workspaces w ON w.id = a.workspace_id"
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
    "   WHERE a.next_slot_at < b.hi"
    "  UNION ALL"
    f"  SELECT s.id, {_NEXT_SLOT.format(after='s.slot')}, s.step + 1"
    "    FROM slots s JOIN acct a ON a.id = s.id CROSS JOIN bounds b"
    "   WHERE s.slot < b.hi AND s.step <= a.eff_ppd * :span_days"
    ")"
    " SELECT 'predicted' AS kind, s.slot AS schedule_slot_at,"
    f"        {_LOCAL_DAY.format(slot='s.slot', tz='a.ws_tz')}, a.eff_tz AS tz,"
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

    ``planned`` holds the stories a person scheduled that are still
    `scheduled`; ``predicted`` the slots the cadence will open (the module
    docstring has how). ``planned_truncated`` and ``predicted_truncated`` say
    a list was cut at its limit. A range wider than
    `vocabulary.RANGE_MAX_DAYS` is cut to that."""
    # A difference, not from_date + the maximum: that sum can pass the last date.
    if (to_date - from_date).days > vocabulary.RANGE_MAX_DAYS:
        to_date = from_date + timedelta(days=vocabulary.RANGE_MAX_DAYS)
    ws = str(workspace_id)
    planned = await readers.rows(
        executor,
        _PLANNED_SQL,
        ws=ws,
        from_date=from_date,
        to_date=to_date,
        lim=int(planned_limit) + 1,
    )
    predicted = await readers.rows(
        executor,
        _PREDICTED_SQL,
        ws=ws,
        from_date=from_date,
        to_date=to_date,
        lead=WALK_LEAD_DAYS,
        # A walk spans the range and the lead, with a part of a day at each
        # end, and a local day holds at most the account's posts per day.
        span_days=(to_date - from_date).days + WALK_LEAD_DAYS + 2,
        lim=int(predicted_limit) + 1,
    )
    return {
        "planned": planned[:planned_limit],
        "planned_truncated": len(planned) > planned_limit,
        "predicted": predicted[:predicted_limit],
        "predicted_truncated": len(predicted) > predicted_limit,
    }
