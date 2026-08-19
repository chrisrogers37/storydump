"""L.2 — the durable counter idiom over `rate_counters` (#859, `02` §6).

`056` shipped the table (composite PK ``(scope, key, window_start)``,
`ck_rate_scope`, `ck_rate_nonneg`); the limit is deliberately NOT in the
database. There is no function, trigger, or CHECK enforcing it — the
advertised-DDL manifest classes the increment-and-check "illustrative" —
because `02` §6 wants the limit compared against the CURRENT config value on
every hit: counters have no refunds, so nothing needs a frozen copy, and "an
abuse limit tightened mid-attack should bite immediately, not next window".
(`04` §L.2's gate says "the frozen limit"; `02` §6 says "deliberately NOT
frozen" and is the domain authority — the contradiction is flagged on #859.)

So THE guard is the statement below — the `WHERE rc.count < :limit` inside the
UPSERT, atomic under the row lock, never check-then-act. This module's only
job is to be the one place that statement is written (`02` §6: "one idiom for
every counter in the system").

Scopes are deliberately not all tenant-shaped (`preauth_ip`, `email_global`,
`tg_global` have no workspace), which is why `p_rate` is role-scoped
``USING (true)`` and nothing here touches tenant GUCs. Every limit and window
length arrives as a parameter — `05` owns the numbers, callers read config and
pass values.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text


def window_start(now: datetime, window_seconds: int) -> datetime:
    """*now* truncated to its fixed window (`02` §6: truncation is the
    caller's job; `05` owns each scope's window length)."""
    if now.tzinfo is None:
        raise ValueError("window_start requires an aware datetime")
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    elapsed = (now - epoch).total_seconds()
    return epoch + timedelta(seconds=int(elapsed // window_seconds) * window_seconds)


async def increment(
    session,
    *,
    scope: str,
    key: str,
    window_start: datetime,
    limit: int,
) -> Optional[int]:
    """One hit against a counter: the `02` §6 increment-and-check, verbatim.

    Returns the post-increment count, or **None when the window is over
    limit** — the consuming site then defers (sender pacing) or rejects
    (admission / pre-auth). Atomic: the first hit inserts at 1, every later
    hit takes the row lock and re-reads the committed count, so concurrent
    callers serialize and exactly `limit` hits succeed per window.

    Runs in the CALLER's transaction and does not commit — an admission
    check's debit belongs in the same transaction as what it admits.
    """
    row = (
        await session.execute(
            text(
                "INSERT INTO rate_counters AS rc (scope, key, window_start, count)"
                " VALUES (:scope, :key, :window_start, 1)"
                " ON CONFLICT (scope, key, window_start)"
                "   DO UPDATE SET count = rc.count + 1 WHERE rc.count < :limit"
                " RETURNING count"
            ),
            {
                "scope": scope,
                "key": key,
                "window_start": window_start,
                "limit": limit,
            },
        )
    ).first()
    return int(row[0]) if row is not None else None
