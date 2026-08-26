"""Is scheduling still advancing? (#1090 F1)

## Why this exists

Two total scheduling outages have now run for 18 and 19 hours and **neither
alerted**. Both were found by someone looking for something else.

- **#1026** — the worker DIED. `service_runs` stopped gaining rows.
- **today** — the worker LIVED. Migration `065` was never applied to production,
  so `ck_jobs_kind` refused a kind the deployed worker mints and every clock tick
  aborted.

They have opposite mechanisms and one observable in common, which is what this
module watches.

## The signal: a DUE CURSOR THAT IS NOT ADVANCING

`fn_clock_tick` selects `WHERE state = 'active' AND next_slot_at IS NOT NULL AND
next_slot_at <= now()` and then ADVANCES the cursor. **All three places in the
whole schema that write `next_slot_at` (059, 062, 063) are that same advance**
(`provisioning`'s module docstring records this). So a cursor sitting in the past
is proof the advance is not happening — and it does not matter why.

That is the point. A MECHANISM detector — "did a tick run", "is the heartbeat
alive" — only catches the failure shape whoever wrote it had in mind. A
heartbeat-absence check is the natural answer to #1026 and it would have been
**blind to today**, because the worker was alive and still writing its heartbeat
while doing no work. Watching the OUTCOME needs no imagination about causes: a
dead worker, an aborting tick, a stuck lane, a cause nobody has thought of yet
all leave the same footprint.

## What it deliberately does NOT do

**It raises nothing.** The check is PULLED, never pushed.

A monitor whose INPUT is produced by the system it monitors cannot detect that
system stopping — that is why this reads cursors rather than job rows. The same
applies one step later to OUTPUT: an alert whose SENDING is performed by the
system it monitors cannot fire when that system is down. So this module answers a
question and an external poller decides whether the answer is an alarm. Silence
then becomes alarming rather than ambiguous, which is #1026's own lesson — a
heartbeat nobody checks for silence is a log line, not a monitor.

The obvious implementation, a system job, would have been wrong twice: it needs a
new `ck_jobs_kind` value (a migration, hard-blocked) and it would share a failure
mode with the thing it watches. The proof is standing: `alert_stranded_sources`,
the alerting job already built, lives in migration `065` and is **un-mintable on
production right now** — dead in exactly the situation it exists to detect.

## Two bounds, stated because they will not be obvious later

**It cannot see a failure PAST the mint.** Cursors advancing while nothing posts
is a different outage and wants its own signal.

**It reads across tenants, and today that works for a reason that is itself a
tracked gap.** The aggregate is estate-wide by nature. Production connects as
`neondb_owner`, which owns these tables and holds BYPASSRLS, so `p_tenant` is
inert (#751). Under the F.4 posture this would need a definer door — a migration.
So the query is correct today and its cross-tenant reach rests on a gap someone
intends to close; whoever closes #751 must give this a door rather than discover
it returning a reassuring zero.

**Nothing identifying is returned** — counts and a lag, never a workspace, an
account or a handle. The endpoint that serves this is unauthenticated by design,
so the aggregate has to be safe to say out loud.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text


async def scheduling_lag(executor) -> dict[str, Any]:
    """How far behind the schedule cursor has fallen, estate-wide.

    ``stalled`` counts active accounts whose slot is due and unadvanced;
    ``max_lag_seconds`` is the worst of them, or ``None`` when none are due.

    A healthy estate answers ``stalled = 0`` *or* a small lag — a cursor is
    briefly due between falling due and the next tick, so a nonzero lag is
    normal and only a SUSTAINED one is an outage. The threshold is the poller's
    to set, deliberately: it depends on the tick cadence, which is deployment
    configuration rather than a fact about this query.

    ``accounts_active`` is returned so a caller can tell "nothing is stalled"
    from "there is nothing to stall" — an estate with no active accounts is
    healthy and also unmonitorable, and those must not read the same.
    """
    row = (
        (
            await executor.execute(
                text(
                    "SELECT"
                    "   count(*) FILTER ("
                    "     WHERE next_slot_at IS NOT NULL AND next_slot_at <= now()"
                    "   ) AS stalled,"
                    "   count(*) AS accounts_active,"
                    # FILTER binds to the AGGREGATE, never to an expression
                    # wrapping it: `EXTRACT(... max(...)) FILTER (...)` is a
                    # syntax error. The seconds are computed inside the max.
                    "   max(EXTRACT(EPOCH FROM now() - next_slot_at)) FILTER ("
                    "     WHERE next_slot_at IS NOT NULL AND next_slot_at <= now()"
                    "   ) AS max_lag_seconds"
                    " FROM ig_accounts WHERE state = 'active'"
                )
            )
        )
        .mappings()
        .one()
    )
    lag = row["max_lag_seconds"]
    return {
        "stalled": int(row["stalled"]),
        "accounts_active": int(row["accounts_active"]),
        "max_lag_seconds": None if lag is None else int(lag),
    }
