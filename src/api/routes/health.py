"""The four health surfaces — Railway's probe, the scheduling axis, the
posting axis (#1090 F1, #1268) and the delivery axis (#1482).

They were the only routes in the app defined inline inside `create_app`; every
other route in the API lives in a module here and is included as a router, and
now so do these. `storydump health` renders the scheduling and posting axes and
`details` (the `OPS_USER_IDS`-only `/api/v1/ops/health`), and each axis has a fleet
monitor polling it (`scripts/*_monitor.py`), so a field renamed here is a renderer
or a poller broken elsewhere.

`details` reads `app.state.*` — the engine, the sampled database role, the pool
watch, the tap counters and the two webhook reports — rather than the factory's
closure, which is the whole reason these can live outside it. Neither it nor
`/health` opens a connection: see `/health`'s docstring. `/health/scheduling`,
`/health/posting`, `/health/delivery` and the operating details' queue read
(`queue_pressure`) do, so each reuses its last answer for `HEALTH_CACHE_SECONDS`
(`AnswerCache`, one per app on `app.state`).

The router carries no `tags=`: these operations have never had one, and
`/openapi.json` is a response body like any other.
"""

from __future__ import annotations

import asyncio
import logging
import time
from types import TracebackType

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError

from src import __version__
from src.services.target import (
    backpressure,
    delivery_health,
    health_reads,
    posting_health,
    scheduling_health,
)
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
#: The commit Railway deployed, which it sets on every Git-triggered deploy.
COMMIT_VAR = "RAILWAY_GIT_COMMIT_SHA"
_START_TIME = time.time()

#: How long `/health/scheduling`, `/health/posting`, `/health/delivery` and the
#: operating details' queue read reuse their last answer. The `/health/*` axes
#: are unauthenticated and each answer takes a connection from the API's
#: shared pool, so without this anyone could drain the pool the webhook needs
#: by polling them. The fleet monitors poll far less often than this, and every
#: number in the payloads is an age or a count that moves on a scale of minutes.
HEALTH_CACHE_SECONDS = 30.0

#: How long the operating details wait for the queue's read before naming it a
#: `TimeoutError`: a database that stops answering hangs a connect for the
#: driver's own minute, and the rest of the details need no database at all.
QUEUE_READ_TIMEOUT_S = 3.0

router = APIRouter()
logger = logging.getLogger(__name__)


class AnswerCache:
    """The last answer of each health surface, reused for `ttl` seconds.

    One per app (`app.state.health_cache`, made by `create_app`), so each app
    a test builds starts empty. A failure is cached like a success: a database
    that raised is not asked again until the window passes, so an outage does
    not turn every poll into a fresh connection attempt. The lock makes the
    requests that arrive while one is computing wait for it rather than each
    opening a connection of their own; each surface has its own, so a slow
    read on one never holds up the other.
    """

    def __init__(self, ttl: float = HEALTH_CACHE_SECONDS, clock=time.monotonic):
        self._ttl = ttl
        self._clock = clock
        self._locks: dict[str, asyncio.Lock] = {}
        # key -> (expires_at, answer, error, its traceback): the answer or the
        # error is set, never both.
        self._entries: dict[
            str, tuple[float, dict | None, Exception | None, TracebackType | None]
        ] = {}

    async def answer(self, key: str, compute):
        """Return `compute()`'s answer for `key`, from the cache while fresh."""
        async with self._locks.setdefault(key, asyncio.Lock()):
            entry = self._entries.get(key)
            if entry is None or self._clock() >= entry[0]:
                try:
                    value, error, tb = await compute(), None, None
                except Exception as exc:  # cached, then re-raised below
                    value, error, tb = None, exc, exc.__traceback__
                entry = (self._clock() + self._ttl, value, error, tb)
                self._entries[key] = entry
        _, value, error, tb = entry
        if error is not None:
            # From the traceback it was caught with: re-raising the one cached
            # object would otherwise append every request's frames to it for
            # the whole window. Its type and origin stay, so a cached pool
            # timeout is still the app's 503.
            raise error.with_traceback(tb)
        return value


@router.get("/health")
async def health_check(request: Request):
    """Railway's probe. No auth, so it says ok, and which version and commit
    answer (what a deploy is verified by), and nothing else. It opens no
    connection — a probe that did would take the service down for a database
    blip no restart repairs — and the operating details it used to carry
    (usage counts, the database login, the pool, the bot's webhook) are
    `details` below, behind `GET /api/v1/ops/health`."""
    return _public(request.app.state)


def _public(state) -> dict:
    return {"status": "ok", "version": VERSION, "commit": state.commit}


def details(state) -> dict:
    """What the API knows about itself, for the people in `OPS_USER_IDS`
    (`routes/ops.py`). Read from `app.state`, so it opens no connection either;
    `target_database` is configuration presence, not liveness."""
    return {
        **_public(state),
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


async def operating_details(state) -> dict:
    """`details`, and the queue's backpressure (phase 3a step 6): the ready
    lanes, the pending outbox and the Telegram pacing, without the waiting
    workspace's id. The queue is the one part that opens a connection, so it
    reuses its last answer like the public axes (`health_cache`), is None
    without an engine, and names a failed or slow read (`QUEUE_READ_TIMEOUT_S`)
    rather than failing or holding up the rest."""
    return {**details(state), "backpressure": await queue_pressure(state)}


async def queue_pressure(state) -> dict | None:
    if state.engine is None:
        return None

    async def read():
        try:
            return await asyncio.wait_for(
                backpressure.read(state.engine, WorkerConfig()), QUEUE_READ_TIMEOUT_S
            )
        except (SQLAlchemyError, OSError, asyncio.TimeoutError, TimeoutError) as exc:
            # a database that refused, failed or did not answer: a report,
            # never a failed read; anything else is a bug and raises
            logger.warning("queue pressure not read: %r", exc)
            return {"error": type(exc).__name__}

    return await state.health_cache.answer("backpressure", read)


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

    async def read():
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
        #
        # Each statement is capped (`health_reads`): a statement past the cap is
        # cancelled, which the app answers as a 503, and frees its connection.
        async with health_reads.connect(engine) as conn:
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
            # Exactly what `scripts/scheduling_monitor.py` reads, and nothing
            # else: the queue's backpressure moved to the operating details
            # (`queue_pressure`), which no monitor has ever read.
            return {**lag, "worker": worker}

    return await request.app.state.health_cache.answer("scheduling", read)


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

    async def read():
        # A DIRECT CONNECTION, not a unit of work, for the reason the route
        # above records: `UnitOfWork.__init__` refuses a blank tenant at
        # construction, and this aggregate is estate-wide and has no tenant.
        # Its cross-tenant reach is 081's doors, the same footing as the route
        # above, and its statements are capped the same way.
        async with health_reads.connect(engine) as conn:
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

    return await request.app.state.health_cache.answer("posting", read)


@router.get("/health/delivery")
async def delivery_health_check(request: Request):
    """Are the messages the product sends getting through? (#1482)

    A FOURTH health surface, by `/health/posting`'s own rule. Deliveries failing
    and posts not landing are independent causes, so folding this axis into
    either payload would rank one against the other, and ranking is what masks.

    The last hour's outbox rows whose last failure fell in it, by class and the
    provider's code, how many of them ended `failed` or sit `ambiguous`, and how
    many rows were sent in the same hour (`delivery_health`, through 101's
    doors). NOTHING IS RAISED HERE, for `/health/scheduling`'s two reasons: the
    alert is `scripts/delivery_monitor.py`, run outside the app. Unauthenticated,
    so AGGREGATES ONLY: counts and codes, never a workspace, a chat or a message.

    503 when the engine is absent, never a reassuring zero.
    """
    engine = request.app.state.engine
    if engine is None:
        raise HTTPException(status_code=503, detail="target database not configured")

    async def read():
        # A direct connection, as on `/health/posting`: the read is estate-wide
        # and has no tenant, its cross-tenant reach is 101's doors, and its
        # statements are capped the same way (`health_reads`).
        async with health_reads.connect(engine) as conn:
            return await delivery_health.outbox_failures(conn)

    return await request.app.state.health_cache.answer("delivery", read)
