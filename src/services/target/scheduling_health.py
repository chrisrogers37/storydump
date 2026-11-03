"""Is scheduling still advancing?

`fn_clock_tick` advances due active account cursors. A cursor that remains in
the past reveals a scheduling failure whether the worker is down or the tick
aborts. Heartbeat presence alone cannot distinguish a working scheduler from
an idle one.

`fn_health_scheduling_lag`, defined in `src/models/target/ddl/04_functions.sql`
and owned by `svc_maintenance`, returns an estate-wide aggregate to
`svc_ingress`. `worker_freshness` reads tenant-free system job rows directly.
Both return counts and ages without workspace or account identifiers.

These reads apply no alert threshold. An external poller interprets them so
the monitor does not depend on the worker it observes. Advancing cursors do
not prove posts land; posting health supplies that separate signal.
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
                    # The door's body (fleet health functions): FILTER binds to the AGGREGATE,
                    # so the seconds are computed inside the max.
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
    monitored axis covers nothing at all until the first tenant arrives.

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
      Catches the failure where **nothing is enqueued at all**: an aborting
      clock tick mints no rows, so a backlog check finds an empty set.
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
                    "   ) AS max_overdue_seconds,"
                    # The commit the freshest success ran, from the claimer
                    # name the worker stamps last (`@<commit>`): NULL for a
                    # row claimed before the stamp, or by an unstamped worker.
                    "   (SELECT substring(j.locked_by FROM '@([0-9a-f]{7})$')"
                    "      FROM jobs j"
                    "     WHERE j.workspace_id IS NULL AND j.state = 'succeeded'"
                    "     ORDER BY j.updated_at DESC LIMIT 1) AS worker_commit"
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
        "commit": row["worker_commit"],
    }
