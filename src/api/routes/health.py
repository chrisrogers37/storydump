"""The three health surfaces — Railway's probe, the scheduling axis and the
posting axis (#1090 F1, #1268).

They were the only routes in the app defined inline inside `create_app`; every
other route in the API lives in a module here and is included as a router, and
now so do these. Nothing about the payloads moved with them: `storydump health`
renders these three and two fleet monitors poll them, so a field renamed here is
a renderer and two pollers broken elsewhere.

Each handler reads `request.app.state.*` — the engine, the sampled database
role, the pool watch, the tap counters and the two webhook reports — rather than
the factory's closure, which is the whole reason they can live outside it. None
of them opens a connection for `/health` itself: see its docstring.

The router carries no `tags=`: these three operations have never had one, and
`/openapi.json` is a response body like any other.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from src import __version__
from src.services.target import backpressure, posting_health, scheduling_health
from src.services.target.work_loop import WorkerConfig

#: The one version string: the OpenAPI document's and `/health`'s. Read by
#: `src/api/app.py` for `FastAPI(version=…)` so the two cannot disagree.
#:
#: Read from the package rather than typed (#1359). The hand-written literal
#: said "0.2.0" from #1035 until 2026-09-21 while `src/__init__.py` reached
#: 1.6.0, so `/health`, the OpenAPI document and `storydump doctor` — which
#: prints it — all reported a version the deployment had not been for months.
#: A number that has to be remembered in two places is a number that drifts.
VERSION = __version__
_START_TIME = time.time()

router = APIRouter()


@router.get("/health")
async def health_check(request: Request):
    """Railway's probe. No auth. `target_database` is configuration
    presence, not liveness — a probe that opened a connection would take
    the service down for a database blip no restart repairs."""
    state = request.app.state
    return {
        "status": "ok",
        "version": VERSION,
        "uptime_seconds": int(time.time() - _START_TIME),
        "target_database": state.engine is not None,
        "db_role": state.db_role,
        # The pool arithmetic and its high-water mark (phase 2 step 4):
        # with `ingress_workers` this is the Σ the `05` inequality reads.
        "pool": (state.pool_watch.snapshot() if state.pool_watch is not None else None),
        "ingress_workers": state.ingress_workers,
        # The tap counters (phase 1 of the 2026-09-09 plan, step 12).
        "taps": state.tap_metrics.snapshot(),
        # The webhook this API registered on the bot at startup — a
        # snapshot from this process's start (`sampled: startup`).
        "webhook": state.webhook,
        "webhook_live": state.webhook_live,
    }


@router.get("/health/scheduling")
async def scheduling_health_check(request: Request):
    """Is scheduling still advancing? (#1090 F1) — a SECOND health surface,
    deliberately not `/health` above.

    Railway gates deploys on `/health`, whose docstring is explicit that a
    probe opening a connection would take the service down for a database
    blip no restart repairs. That is right, and it is exactly why #1026 asked
    for a separate dependency-touching check: liveness and "is the work
    happening" are different questions and one endpoint cannot answer both
    without making one of them wrong.

    NOTHING IS RAISED HERE. This reports; the FLEET alert path polls it and
    decides. Two independent reasons, and the second is measured:

    1. An alert whose SENDING is performed by the system it monitors cannot
       fire when that system is down — the same law that kept this detector
       off the job table, applied to the output side.
    2. The app's own notification routing has NO WRITER: nothing anywhere
       writes `channel_bindings`, for any workspace (navi). An alert
       delivered into it would vanish silently, and we would have built a
       detector whose output goes nowhere.

    Unauthenticated, so it answers in AGGREGATES ONLY — counts and a lag,
    never a workspace, an account or a handle.

    503 when the engine is absent, matching every other data route: a
    monitor must be able to tell "scheduling is fine" from "I could not
    look", and collapsing those is the failure this whole issue is about.
    """
    engine = request.app.state.engine
    if engine is None:
        raise HTTPException(status_code=503, detail="target database not configured")
    # A DIRECT CONNECTION, not a unit of work, and the empty tenant string
    # this replaced was not a near-miss — `UnitOfWork.__init__` refuses a
    # blank tenant at CONSTRUCTION, so the route raised before touching the
    # database and returned 500 to every caller it ever had.
    #
    # The guard is right and must not move. This aggregate is estate-wide
    # and has no tenant; naming one that does not exist is a lie the guard
    # correctly refused, and the remedy is the one its own message gives.
    #
    # The estate-wide reads answer through doors (081, `07` §24): each
    # is a SECURITY DEFINER function owned by `svc_maintenance`, so the
    # answer is the same under the owner login and under `svc_ingress`.
    # The first switch to `svc_ingress` (2026-09-20, #751) is why: with
    # the reads still direct, every policy-covered table read empty and
    # this surface said `no-signal` for a live estate.
    async with engine.connect() as conn:
        # TWO AXES, ONE PAYLOAD (#1120). The cursor axis is empty whenever
        # no destination is active, and `no-signal` is then the answer
        # whether the worker is healthy or DEAD — so the one monitored axis
        # covered nothing at all until the first tenant arrived. The worker
        # axis reads system jobs, whose population is tenant-independent.
        #
        # Same endpoint rather than a sibling, deliberately: a second URL
        # would need a second poller invocation enrolled on the fleet host,
        # a unit change, to close a hole the existing poller can already
        # reach. The cursor keys keep their names and meanings, so a poller
        # predating this change reads the payload exactly as before.
        lag = await scheduling_health.scheduling_lag(conn)
        worker = await scheduling_health.worker_freshness(conn)
        # The backpressure signal (phase 3a step 6): the same numbers the
        # worker's status line prints, for the poller that watches this —
        # without the waiting workspace's id (this route is public and
        # promises nothing identifying; `identify` stays False).
        pressure = await backpressure.snapshot(
            conn,
            now=datetime.now(timezone.utc),
            global_limit=WorkerConfig().global_limit,
            global_window_seconds=WorkerConfig().global_window_seconds,
        )
        return {**lag, "worker": worker, "backpressure": pressure}


@router.get("/health/posting")
async def posting_health_check(request: Request):
    """Did a post actually LAND? (#1268) — a THIRD health surface.

    `/health/scheduling` above reads the clock and the worker. Both stayed
    true through a sixteen-day silence in which nothing posted: 1936
    consecutive `healthy` readings, every field of them correct. An intent
    awaiting approval is not overdue, it is waiting correctly, so the
    machinery gauge reads healthy because the machinery IS healthy.

    A SEPARATE URL — and the rule is stated here rather than re-argued,
    because the issue this closes names the next axes (stranded approvals,
    an empty media pool, an undelivered outbox) and each will ask again.

    **An axis JOINS an existing payload when it can share that poller's
    single verdict. It gets its OWN surface when it would have to be RANKED
    against an existing one.**

    Ranking is what masks. `scheduling_monitor.classify` returns
    `WORKER_DOWN` "FIRST, and above every cursor reading" — correct there,
    and it means a stalled cursor is unreportable while the worker is down.
    That is a fair trade for two axes that share a cause. It is not one
    here: "nothing posted" and "the clock stopped" are the pair that was
    *observed to disagree for sixteen days*, so whichever lost the ranking
    would be the one silenced, and this axis exists precisely because the
    other read healthy.

    The tempting discriminator — "it needs its own verdict, thresholds and
    state file" — does NOT hold, and is recorded as rejected so it is not
    reached for again: the worker axis has its own verdict states and two
    thresholds of its own, and was folded in anyway.

    NOTHING IS RAISED HERE, for the two reasons `/health/scheduling` gives
    verbatim: an alert whose sending is performed by the system it monitors
    cannot fire when that system is down, and the app's own notification
    routing has no writer, so an alert delivered there would vanish.

    Unauthenticated, so it answers in AGGREGATES ONLY — counts and ages,
    never a workspace, an account, a handle or a permalink.

    503 when the engine is absent: "posting is fine" and "I could not look"
    must never collapse, which is the whole subject of the issue this
    closes.
    """
    engine = request.app.state.engine
    if engine is None:
        raise HTTPException(status_code=503, detail="target database not configured")
    # A DIRECT CONNECTION, not a unit of work, for the reason the route
    # above records: `UnitOfWork.__init__` refuses a blank tenant at
    # construction, and this aggregate is estate-wide and has no tenant.
    # Its cross-tenant reach is 081's doors, the same footing as the route
    # above.
    async with engine.connect() as conn:
        posting = await posting_health.posting_freshness(conn)
        attempts = await posting_health.publish_attempts(conn)
        # `accounts_active` is CONTEXT for the alert text and never a gate:
        # a poller excused from speaking by a zero here would excuse an
        # empty tier forever, which is the first half of the outage this
        # endpoint exists for. The age beside it is the opposite — an
        # anchor that can only make the poller speak sooner.
        dests = await posting_health.destinations(conn)
        # Every key spelled in the service that computes it, so a rename
        # cannot leave the route publishing a name nothing produces.
        return {**posting, **attempts, **dests}
