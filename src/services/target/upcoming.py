"""What is coming on the calendar: for a range of the workspace's local days,
the planned stories still scheduled, the slots the cadence will open, and the
later occurrences of each repeating series.

## Predicted slots are projected, never read

A cadence story exists only once its slot comes due. The clock mints a
`plan_slot` job when an account's cursor (`ig_accounts.next_slot_at`) is due,
and advances the cursor with `fn_next_slot` in the same statement, so the days
ahead hold no cadence rows to read.

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

## A series' later occurrences are projected too

A repeating planned story (#485) has one live occurrence at a time, and the
next is made only when it ends (`tg_intent_repeat`). The read projects the rest
from the live story's rule with `fn_repeat_times`, the grid the trigger makes
them from, starting just after the story's own time, as the trigger does. The
story's own time is its own row. After a post the trigger starts from the post's
next local day instead (a series posts at most once a local day), which skips
nothing here: a series has one time a day, and a story posted at its own time
was that day's. A series the trigger would not carry on, its workspace
offboarding or its account no longer live, projects nothing. Nor does a series:
- whose story is parked for review: its next days hang on how the review ends,
  and a story given up late makes its next occurrence born late and missed;
- whose workspace is not posting (paused, or suspended): the miss sweep would
  end each occurrence as `paused`, and the cadence's predicted slots are hidden
  then too.

## A projection is not a story

Every such row says ``kind: predicted`` or ``kind: repeat`` and carries no
state. It is a place, not a promise: `plan_slot` mints nothing for a slot with
no eligible media, and a series can be stopped or moved before its next
occurrence comes. A time that has come is left out, because the clock is
minting it, or has.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from src.services.target import intent_ledger, readers, vocabulary
from src.services.target.content_runway import (
    _POSTING_SQL,
    _POSTS_PER_DAY_SQL,
    _WORKSPACE_POSTING_SQL,
)
from src.services.target.workspaces import (
    _INTENT_COLUMNS,
    _INTENT_FROM,
    LOCAL_DAY_SQL,
    LOCAL_MIDNIGHT_SQL,
    _archive_row,
    _picture_sql,
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

#: The most later occurrences one read returns: a hundred daily series across
#: the widest range. A series comes at most once a day, at its one time.
REPEATING_MAX = vocabulary.RANGE_MAX_DAYS * 100

#: The range's two bounds as instants, as the intents read bounds a slot.
_RANGE_START = LOCAL_MIDNIGHT_SQL.format(name="from_date")
_RANGE_END = LOCAL_MIDNIGHT_SQL.format(name="to_date")

#: The intents read's row and its day. The row's own column list, so what the
#: queue's row gains the calendar's gains, and read through the same
#: `_archive_row`.
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


#: The range's bounds in the joined workspace's zone: `_RANGE_START` and
#: `_RANGE_END` without their sub-select. A sub-select among a set-returning
#: SQL function's arguments keeps the planner from inlining it, and the grid
#: would then be planned once per series.
_SERIES_FROM = "(CAST(:from_date AS date)::timestamp AT TIME ZONE w.tz)"
_SERIES_TO = "(CAST(:to_date AS date)::timestamp AT TIME ZONE w.tz)"

#: Each live repeating story's later occurrences in the range (#485), asked of
#: its grid as the successor trigger asks it, where the trigger would make them
#: (the module docstring has why). A story at or after the range's end has none
#: in it. `NOT_TERMINAL` is spelled on a bare `state`, so it takes the story's
#: alias here, among four tables that each have one.
_REPEATING_SQL = (
    "SELECT 'repeat' AS kind, t.slot AS schedule_slot_at,"
    f" {LOCAL_DAY_SQL.format(slot='t.slot', tz='w.tz')},"
    " COALESCE(a.tz, w.tz) AS tz, i.id AS intent_id, i.ig_account_id,"
    " i.media_item_id, m.file_name, m.media_kind, m.category,"
    f" {_picture_sql('m.')},"
    f" {intent_ledger.repeat_sql('i.')} AS repeat"
    f"{_INTENT_FROM}"
    "  CROSS JOIN LATERAL fn_repeat_times(i.repeat_unit, i.repeat_every,"
    "      i.repeat_days, i.repeat_at, i.schedule_slot_at, COALESCE(a.tz, w.tz),"
    f"      GREATEST({_SERIES_FROM}, i.schedule_slot_at + interval '1 microsecond'),"
    f"      {_SERIES_TO}) AS t (slot)"
    " WHERE i.workspace_id = :ws AND i.origin = 'planned'"
    f"   AND i.repeat_unit IS NOT NULL AND i.{intent_ledger.NOT_TERMINAL}"
    "   AND i.state <> 'review_required'"
    f"   AND i.schedule_slot_at < {_SERIES_TO}"
    f"   AND {_WORKSPACE_POSTING_SQL} AND {intent_ledger.LIVE_ACCOUNT}"
    "   AND t.slot > now()"
    " ORDER BY t.slot, i.id LIMIT :lim"
)


async def upcoming(
    executor,
    *,
    workspace_id: str,
    from_date: date,
    to_date: date,
    planned_limit: int = PLANNED_MAX,
    predicted_limit: int = PREDICTED_MAX,
    repeating_limit: int = REPEATING_MAX,
) -> dict[str, Any]:
    """The planned stories, the predicted slots and the series' later
    occurrences of the workspace's local days ``[from_date, to_date)``, each
    list soonest first.

    **The range** is local days in the WORKSPACE's zone, at most
    `vocabulary.RANGE_MAX_DAYS` of them: a wider one is cut to that.

    **``planned``** holds the stories a person scheduled that are still
    `scheduled`. A row is the intents read's row (`workspaces._INTENT_COLUMNS`,
    an archive item's provenance and name included) and its ``day``, so the
    page draws it as it draws a queue row.

    **``predicted``** holds the slots the cadence will open (the module
    docstring has how). A row carries ``kind`` and the keys the two kinds
    share: ``schedule_slot_at``, ``day``, ``tz``, and the account by id, handle
    and display name.

    **``repeating``** holds each repeating series' later occurrences (#485,
    the module docstring has how). A row carries ``kind``, ``schedule_slot_at``,
    ``day`` and ``tz``, the live story it is projected from (``intent_id``), its
    item and account, its picture's fields and category, and its ``repeat``
    rule.

    A row's ``day`` is its slot's date in the workspace's zone, and its ``tz``
    the zone its time reads in: the account's, else the workspace's.

    **Bounds.** ``planned_truncated``, ``predicted_truncated`` and
    ``repeating_truncated`` say a list was cut at its limit. A cut list is whole up to its last row's ``day``,
    which may itself be short. The limits bound the answer, not the work, which
    is one walk for each account the clock posts for and one grid for each live
    repeating story; a walk takes no more steps than the days it spans hold
    slots, which cuts no walk and stops one that no longer advances."""
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
    repeating = await readers.rows(
        executor, _REPEATING_SQL, lim=int(repeating_limit) + 1, **bounds
    )
    # Each list read one over its limit: kept to the limit, and said when cut.
    out: dict[str, Any] = {}
    for name, rows, limit in (
        ("planned", planned, planned_limit),
        ("predicted", predicted, predicted_limit),
        ("repeating", repeating, repeating_limit),
    ):
        out[name], out[f"{name}_truncated"] = rows[:limit], len(rows) > limit
    # A story's picture and provenance go through the Queue's projection, so
    # an archive story is named for where it came from, never by its id.
    for name in ("planned", "repeating"):
        out[name] = [_archive_row(row) for row in out[name]]
    return out
