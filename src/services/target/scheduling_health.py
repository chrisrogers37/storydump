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

**It reads across tenants through a door.** The aggregate is estate-wide by
nature, and `scheduling_lag` reads it through `fn_health_scheduling_lag`, a
SECURITY DEFINER function owned by `svc_maintenance` (081, `07` §24). The
reassuring zero this paragraph once warned of was met in production: the first
switch of the API to `svc_ingress` (2026-09-20, #751) read `accounts_active 0`
for two active accounts and the poller's verdict became `no-signal`, which it
never pages on. `worker_freshness` stays a direct read: its rows carry
`workspace_id IS NULL` and `p_jobs` admits them to every runtime login.
`tests/scripts/test_fleet_health_doors_gate.py` runs both as `svc_ingress`.

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
                    # The door's body is this module's former query, verbatim
                    # (081): FILTER binds to the AGGREGATE, so the seconds are
                    # computed inside the max.
                    "SELECT o_stalled AS stalled,"
                    "   o_accounts_active AS accounts_active,"
                    "   o_max_lag_seconds AS max_lag_seconds"
                    " FROM fn_health_scheduling_lag()"
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


async def worker_freshness(executor) -> dict[str, Any]:
    """Is the WORKER still finishing jobs? — the axis that survives an empty estate.

    `scheduling_lag` above reads `ig_accounts WHERE state = 'active'`. On an
    estate with no destinations that population is empty, so it answers
    `no-signal` — correctly, and the poller says so. But `no-signal` is then the
    answer whether the worker is healthy or **dead**, which means the one
    monitored axis covers nothing at all until the first tenant arrives (#1120).

    This reads the population that is NOT empty. System jobs run on a cadence
    regardless of how many tenants exist: production carried 78 successes over
    an unbroken 6-hourly cadence while holding zero workspaces.

    ## The population is the SCHEMA's definition, not a list kept here

    `ck_jobs_system_kinds` is a biconditional —
    ``(workspace_id IS NULL) = (kind = ANY(ARRAY[...]))`` — so
    ``workspace_id IS NULL`` *is* "the tenant-independent kinds", enforced by the
    database. A literal kind list here would be a second copy of that set, and
    the copy would be the one that goes stale when a system kind is added.
    Not every system kind recurs (`send_email` is enqueued on demand); the
    on-demand ones can only ever make the estate look FRESHER, never staler, so
    they cannot manufacture an alert.

    ## Two signals, because each is blind where the other sees

    Both are returned; neither is a verdict. **The threshold is the poller's**,
    the same split `scheduling_lag` makes and for the same reason — the cadence
    is deployment configuration, not a fact about this query.

    - ``last_success_age_seconds`` — how long since any system job finished.
      Catches the failure where **nothing is enqueued at all**: #1026's second
      instance aborted the clock tick, so no rows were minted and a backlog
      check would have had an empty set to look at.
    - ``overdue_ready`` / ``max_overdue_seconds`` — due jobs nobody claimed.
      Catches a worker that is alive and minting but not *working* — a stuck
      lane, a poisoned claim — where recent successes from other kinds keep the
      age signal quiet.

    A dead worker trips both. That is the point: the check is OUTCOME-shaped, so
    it does not need to know which mechanism failed.

    ## What it returns when it CANNOT tell, which must not read as healthy

    ``last_success_age_seconds`` is ``None`` when no system job has **ever**
    succeeded, and that is deliberately not ``0``. Zero is the *most reassuring
    value the field has* — "a job finished just now" — and it would be returned
    by a system that has never run once. The whole defect class this instrument
    exists for is a reading that fails toward good news, and the instrument must
    not be an instance of it. ``max_overdue_seconds`` is ``None`` on the same
    rule: nothing overdue and no backlog to measure are different facts.

    ``succeeded_ever`` is returned so a caller can tell "fresh" from "there has
    never been anything to be fresh" without inferring it from a null — the same
    role `accounts_active` plays for `scheduling_lag`.

    **Where this is blind, stated because it will not be obvious later.** Both
    signals read one table, so an unreadable `jobs` is not a healthy estate and
    the route's 503 must carry that (it does). And a worker that dies *before*
    the very first system job is minted leaves no baseline and nothing overdue —
    genuinely unknowable from here, which is why `None` is a distinct answer
    rather than a large age.

    Aggregates only: counts and two ages, never a workspace, a kind or a payload.
    The endpoint is unauthenticated by design.
    """
    row = (
        (
            await executor.execute(
                text(
                    "SELECT"
                    "   count(*) FILTER (WHERE state = 'succeeded')"
                    "     AS succeeded_ever,"
                    # min-of-ages, not max-of-times: FILTER binds to the
                    # AGGREGATE, so `EXTRACT(... max(...)) FILTER (...)` is a
                    # syntax error. The freshest success is the SMALLEST age.
                    "   min(EXTRACT(EPOCH FROM now() - updated_at))"
                    "     FILTER (WHERE state = 'succeeded')"
                    "     AS last_success_age_seconds,"
                    "   count(*) FILTER ("
                    "     WHERE state = 'ready' AND run_at <= now()"
                    "   ) AS overdue_ready,"
                    "   max(EXTRACT(EPOCH FROM now() - run_at)) FILTER ("
                    "     WHERE state = 'ready' AND run_at <= now()"
                    "   ) AS max_overdue_seconds"
                    " FROM jobs WHERE workspace_id IS NULL"
                )
            )
        )
        .mappings()
        .one()
    )
    age = row["last_success_age_seconds"]
    overdue = row["max_overdue_seconds"]
    return {
        "succeeded_ever": int(row["succeeded_ever"]),
        "last_success_age_seconds": None if age is None else int(age),
        "overdue_ready": int(row["overdue_ready"]),
        "max_overdue_seconds": None if overdue is None else int(overdue),
    }
