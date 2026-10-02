"""Are the messages the product sends getting through? (#1482)

The read behind ``GET /health/delivery``, and its fourth health surface.

## The axis nothing watched

`settle` (``outbox.py``) sorts every failed send by type: a 429 goes back to
`pending`, a gone destination, a refused message and a dead credential end
`failed`, anything else is `ambiguous`. Until migration 090 it then wrote the
state and nothing else, so a burst of failures left no cause in the database.
Production's 21 failed rows
of 2026-09-12 (all card edits, 16 of them in one hour) cannot say today whether
the chat was gone, the message refused or the token dead. Nothing counted
failures either, so nothing could alert on them. 090 records the class, the
provider's code and the time on the row. This module counts them.

## Its own surface, by `/health/posting`'s rule

"An axis joins an existing payload when it can share that poller's single
verdict, and gets its own surface when it would have to be ranked against an
existing one." Deliveries failing and posts not landing are independent causes:
cards can fail while posts land, and posts can stop while every card goes out.
Folding this into either payload would make one mask the other.

## What it returns, and what it does not decide

- The rows whose LAST failure falls in the window, by class and code, and how
  many of them are ``alerting``: ended `failed`, or sitting `ambiguous`. A 429
  is a deferral, not a failure. It is counted as context and never alerts.
- The rows SENT in the same window. An hour with no failures and no traffic is
  not an hour that delivered (#1120); the poller reads the count against it.
- Counts only, never a workspace, a chat or a message: the route is
  unauthenticated.

No threshold is applied here. The poller (``scripts/delivery_monitor.py``) owns
it, the split `posting_health` and `scheduling_health` make.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

#: The window a reading covers: one hour, the unit the alert is stated in. It
#: sits inside the [60 s, 24 h] the doors clamp to, so it is the window used.
WINDOW_SECONDS = 3600


async def outbox_failures(executor) -> dict[str, Any]:
    """The last hour of delivery failures and deliveries, estate-wide, through
    090's two doors.

    ``by_class`` maps each failure class to ``{rows, alerting, codes}``, with
    ``codes`` keyed by the provider's code as a string (``"none"`` when no
    answer came back). ``failed_or_ambiguous`` is the sum of ``alerting``: the
    number the poller alerts on.
    """
    params = {"w": WINDOW_SECONDS}
    rows = (
        (
            await executor.execute(
                text(
                    "SELECT o_failure_class AS failure_class,"
                    "   o_error_code AS error_code,"
                    "   o_rows AS rows,"
                    "   o_alerting_rows AS alerting"
                    " FROM fn_health_outbox_failures(CAST(:w AS integer))"
                ),
                params,
            )
        )
        .mappings()
        .all()
    )
    sent = (
        await executor.execute(
            text("SELECT fn_health_outbox_sent(CAST(:w AS integer)) AS sent"), params
        )
    ).scalar_one()
    by_class: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = by_class.setdefault(
            str(row["failure_class"]), {"rows": 0, "alerting": 0, "codes": {}}
        )
        entry["rows"] += int(row["rows"])
        entry["alerting"] += int(row["alerting"])
        code = "none" if row["error_code"] is None else str(int(row["error_code"]))
        # The door groups by (class, code): each code arrives once per class.
        entry["codes"][code] = int(row["rows"])
    return {
        "window_seconds": WINDOW_SECONDS,
        "sent_in_window": int(sent),
        "failed_or_ambiguous": sum(e["alerting"] for e in by_class.values()),
        "by_class": by_class,
    }
