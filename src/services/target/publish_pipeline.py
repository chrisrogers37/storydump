"""L.5 slice 2 — the checkpointed publish-pipeline executor body (#915/#862).

The `publish_pipeline` job kind's executor: claimed via `fn_claim_job` by the
composition root (absent until L.7/M — this module is the callable it will
dispatch to), run here as a transaction-per-checkpoint ladder over
``post_intents.publish_step``, finalized via `finalize_job` in the terminal
domain transaction.

## The ladder, and where each transaction ends

`02` §5, normative: a database transaction NEVER spans a provider call —
"open, write the checkpoint/permit, commit, then talk to the provider". The
egress floor enforces it from the other side for floor-routed calls; the
injected adapters here are called with no UoW open, which the gate proves by
running the real ladder.

    [flip tx]     approved → publishing (§4 CTE: cap debit + key-4), step none
    ...           media_fetch + transit.upload            (no tx open)
    [checkpoint]  transit_asset_ref + step transit_uploaded
    [permit tx]   container_create permit (lease CAS inside acquire_permit)
    ...           meta.create_container                   (no tx open)
    [checkpoint]  resolve permit + ig_container_id + step container_created  (R1)
    ...           meta.container_status poll              (no tx open)
    [checkpoint]  step container_ready
    [permit tx]   publish permit — acquire_permit itself advances step
                  publish_called (the permit IS the durable record, §6)
    ...           meta.publish                            (no tx open)
    [terminal tx] resolve permit + state posted + effect_confirmed +
                  ig_media_id + times_posted/recent-lock/last_posted_at +
                  finalize_job — ONE transaction (`02` §5: "the domain
                  transaction and job finalization commit together")
    ...           best-effort transit.destroy (FC-3.5)    (after commit)

Every checkpoint transaction re-CASes the lease (`assert_lease`, §6 step 3),
so a fenced worker can neither advance the ladder nor record outcomes.

## Ordering decisions that are derivations, not choices

**Pre-check → flip → transit → container → publish.** `02` §8 places the
advisory pre-check "immediately before the §4 flip transaction" AND values it
as "skipping doomed container/transit work" — both hold only if the flip
precedes that work. The intent is `publishing` throughout the ladder.

**A story never waits inside the slot (the float, plan 03, 2026-09-14).**
Every wait between attempts — a refused fetch's round spent, a container Meta
cannot find, a rate limit, a pending poll, error 9 — steps the intent back
`publishing → approved` (edge 076) with `publish_step`, the transit asset and
the cap debit intact, so key 4 is held only for the seconds of a call and any
number of stories wait side by side; re-entry is `flip_to_publishing`, made
re-entrant (no second debit). Error 9's permit resolves `failed` (Meta saying
the publish definitively did not happen — §6's "confirmed-safe failure"), so
the next attempt permits generation+1 and calls again, and the card says when
("✅ Approved · posts tomorrow 09:00").

**A deferral restores the attempt the claim consumed** (`reschedule_job`
docstring carries the §4/§6 derivation); a retryable failure keeps it, and
attempts exhausted on a retryable class is G5 poison → `review_required`
(debit retained — `02` §4: only resolve-failed refunds from there).

**Ambiguity is typed, not parsed**: an exception from `meta.publish` that is
NOT a :class:`MetaError` is a lost response — the permit stays unresolved in
spirit and is marked `ambiguous` via the same `resume_unresolved` door a
crash-resume uses; the intent parks `publishing_ambiguous`, ZERO retries, the
L.3 reconciler owns it (R8). Anything else the adapter answered definitively.

**Generations derive from `provider_operations`** (MAX per op kind), never
from a second counter: the ops table is the durable record, and
`uq_ops_business_key` makes a wrong derivation loud instead of double-calling.

## What this module deliberately does not do

No claim loop, no process root, no adapter construction and no heartbeat
wiring — all four belong to `src/worker.py`, which is the composition root
that did not exist when this module was written (#903, closed 2026-08-21) and
which now wires the REAL Graph adapter (`instagram_graph`) into the seam
`meta_adapter` declares. What this module owns is the ladder between them:
it is handed `meta` and never builds one, which is why the same body runs
against the stub in the L.5 gate and against Meta in production
(#1325 audit, TD-A20). Unexpected
exceptions PROPAGATE — a crashed worker must look crashed, so the lease
expires, the reaper re-readies, and resume-from-checkpoint does its job;
a blanket except here would convert crash-safety into silent corruption.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import text

from src.exceptions.base import StorydumpError
from src.services.target import (
    audit,
    intent_ledger,
    jobs,
    outbox,
    prompts,
    provider_ops,
    publish_cap,
)
from src.services.target.drive_adapter import (
    DriveError,
    DriveLostResponse,
    DriveTerminalError,
)
from src.services.target.jobs import assert_lease, finalize_job, reschedule_job
from src.services.target.meta_adapter import (
    OAUTH_ERROR_CODE,
    MetaCapDeferral,
    MetaError,
    MetaLostResponse,
    MetaRetryableError,
    MetaTerminalError,
)
from src.services.target.publish_cap import FlipOutcome, IntentNotApproved
from src.services.target.unit_of_work import apply_gucs, unit_of_work
from src.services.target.usage_precheck import DEFER
from src.utils.datetime_utils import ms_since, utcnow

logger = logging.getLogger(__name__)

#: `05` row 8, bulk lane: backoff 1/5/15/60 min for retryable failures — the
#: ladder `jobs.BACKOFF_SECONDS` owns, as floats because this pipeline's rungs
#: go straight into `timedelta(seconds=…)` (#1325 audit, TD-A5).
DEFAULT_BACKOFF_SECONDS = tuple(float(s) for s in jobs.BACKOFF_SECONDS["bulk"])

#: For rendering a retention window in days.
SECONDS_PER_DAY = 86400

#: Readiness poll: bounded segments so a lease (bulk 120 s) never covers an
#: unbounded wait — "container poll segments ≤ 60 s" is `05`'s lease rationale.
#: PINNED, not parameters (the L.0 seam rule: leaving half an inequality
#: configurable is the gap) — a composition root must not be able to breach
#: the lease arithmetic silently. Worst case: 4 × 2 s of sleep per segment.
POLL_INTERVAL_S = 2.0
POLL_BUDGET = 5

#: Container status vocabulary (0.4-verified, `02` §6). Before the publish
#: call FINISHED means "ready to be published"; PUBLISHED cannot precede a
#: publish permit and is treated as ready so the publish call arbitrates.
_READY_STATUSES = ("FINISHED", "PUBLISHED")
_DEAD_STATUSES = ("ERROR", "EXPIRED")

# Outcome vocabulary — what one run of the executor did, for logs and tests.
POSTED = "posted"
#: Meta's "the media could not be fetched from this uri" (9004/2207052): on a
#: first container attempt it is the fetch racing the asset's first serving.
FETCH_FAILED_CODE = 9004
DEFERRED_CAP = "deferred_cap"
#: Key 4 (`uq_publish_exclusive`): a sibling of the real account is publishing
#: right now. The story waits this long and tries again — seconds, because a
#: publish takes seconds; a cap denial waits for the next product slot
#: instead, which is hours (adversarial review of #1301).
DEFERRED_BUSY = "deferred_busy"
#: A busy re-check backs off (the float, plan 03): a flat 20 s behind a long
#: hold would turn N waiting siblings into thousands of claims a day each.
BUSY_RETRY_SECONDS = (20, 40, 60)
#: The float (plan 03 of the first-fetch investigation, 2026-09-14). Meta's
#: "media could not be fetched" (9004) is a refusal of a fresh asset's first
#: fetch about a quarter of the time, remembered per url for a minute or two,
#: never the file. A refused fetch is retried AT ONCE with a url Meta has
#: never seen (`FRESH_URLS_PER_ROUND` per run), then the story steps back out
#: of the account's slot and waits its own ladder — six waits, 33.5 minutes —
#: before the workspace's review card. The waits are counted on the story
#: (`attempts_by_step.fetch_waits`), never on the job's attempts.
FRESH_URLS_PER_ROUND = 3
FETCH_RETRY_SECONDS = (30, 60, 120, 300, 600, 900)
#: Meta's "The requested resource does not exist" (24/2207006) at the publish
#: call, about a container it had just reported ready (2026-09-13 23:18): the
#: container is recreated from the same upload after a short wait — never
#: re-published by an id Meta says it cannot find (plan 03 D4).
CONTAINER_GONE_CODE = 24
CONTAINER_GONE_RETRY_SECONDS = (10, 30, 60)
DEFERRED_META_CAP = "deferred_meta_cap"
PARKED_AMBIGUOUS = "parked_ambiguous"
RETRY_SCHEDULED = "retry_scheduled"
POISONED = "poisoned"
FAILED = "failed"
CANCELLED = "cancelled"
#: The workspace is paused (Settings › General): the job waits, the intent
#: stays `approved`, no cap is spent. Re-checked every `PAUSE_RECHECK_SECONDS`.
DEFERRED_PAUSED = "deferred_paused"
#: Dry run (Settings › General): the intent completes as if published —
#: the cap, the rotation, the card's line — and nothing reaches Instagram.
POSTED_DRY_RUN = "posted_dry_run"
PAUSE_RECHECK_SECONDS = 300


class _Ctx:
    """One run's working set: the job, the intent row, account/workspace
    effective config, and permit state — read once, threaded explicitly."""

    def __init__(self, job: dict, intent: dict, ops: list[dict]):
        self.job = job
        self.intent = intent
        self.ops = ops
        raw = intent.get("attempts_by_step")
        if isinstance(raw, str):
            raw = json.loads(raw)
        intent["attempts_by_step"] = dict(raw or {"v": 1})
        #: The typed error a readiness poll met when it answers "unauthorized",
        #: so the caller can record it on the intent.
        self.poll_error: Optional[BaseException] = None
        #: The job payload's dry-run snapshot (see `_load`).
        self.dry_run: bool = False

    @property
    def intent_id(self) -> str:
        return str(self.intent["id"])

    @property
    def workspace_id(self) -> str:
        return str(self.job["workspace_id"])

    @property
    def tz(self) -> str:
        """The story's effective zone. `_load`'s SELECT always projects
        `eff_tz` (`COALESCE(a.tz, w.tz)`, :395), so the fallback is for a NULL
        column, not a missing key."""
        return str(self.intent.get("eff_tz") or "UTC")

    def count(self, key: str) -> int:
        """A per-class counter on the story (`attempts_by_step`, the float)."""
        return int((self.intent.get("attempts_by_step") or {}).get(key) or 0)

    def counters(self) -> dict:
        return dict(self.intent.get("attempts_by_step") or {"v": 1})

    def unresolved(self, op_kind: str) -> Optional[dict]:
        for op in self.ops:
            if op["op_kind"] == op_kind and op["state"] == "permitted":
                return op
        return None

    def next_generation(self, op_kind: str) -> int:
        gens = [op["generation"] for op in self.ops if op["op_kind"] == op_kind]
        return (max(gens) + 1) if gens else 1

    def latest(self, op_kind: str) -> Optional[dict]:
        rows = [op for op in self.ops if op["op_kind"] == op_kind]
        return max(rows, key=lambda r: r["generation"]) if rows else None


@asynccontextmanager
async def _leased_tx(uow, job: dict):
    """One checkpoint transaction: a UoW tx that FIRST re-CASes the lease
    (`02` §6 step 3), so a fenced worker cannot write past this line — or
    reach the provider work behind it. The bare ``uow.begin()`` blocks left
    in this module are the ones whose writes carry their OWN lease CAS
    (`finalize_job`); the distinction is deliberate and now visible.
    """
    async with uow.begin() as session:
        await assert_lease(session, job["id"], job["lease_token"])
        yield session


class TransitNotReady(StorydumpError):
    """The story frame did not serve within the readiness budget."""


async def run_publish_pipeline(
    job: dict,
    *,
    engine,
    meta,
    transit,
    media_fetch: Callable[[dict], Any],
    precheck=None,
    backoff_seconds: tuple = DEFAULT_BACKOFF_SECONDS,
    repost_ttl_days_default: int = 7,
    now_fn: Callable[[], datetime] = utcnow,
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> str:
    """Drive one claimed `publish_pipeline` job to this run's outcome.

    *job* is the claimed row (as `fn_claim_job` returns it). *meta*,
    *transit* and *media_fetch* are the injected provider seams (module
    docstring). Returns one of the outcome constants; raises on anything
    unexpected — a crashed run must look crashed.
    """
    uow = unit_of_work(engine, str(job["workspace_id"]), actor_kind="system")
    ctx = await _load(uow, job)
    if ctx is None:
        async with uow.begin() as session:
            await finalize_job(session, job["id"], job["lease_token"], "failed")
        return FAILED

    state = ctx.intent["state"]

    # -- routing by intent state ------------------------------------------------
    if state in (*intent_ledger.TERMINAL_STATES, "review_required"):
        # Terminal, or operator-owned (review_required): this job has nothing
        # to execute. finalize_job's own token CAS is the fence here.
        async with uow.begin() as session:
            await finalize_job(session, job["id"], job["lease_token"], "cancelled")
        return CANCELLED
    if state == "publishing_ambiguous":
        # The reconciler owns it; this job's resume duty is already done.
        async with uow.begin() as session:
            await finalize_job(session, job["id"], job["lease_token"], "succeeded")
        return PARKED_AMBIGUOUS
    if state not in ("approved", "publishing"):
        raise ValueError(f"intent {ctx.intent_id} in unexpected state {state!r}")

    if state == "approved":
        if ctx.intent.get("is_paused") and not ctx.intent["cancel_requested"]:
            # Pause Posting holds an approved intent here, before the cap is
            # spent: the job waits and re-checks; the card keeps its line. A
            # cancel asked for meanwhile is honoured by `_admit`, not held.
            return await _defer_paused(uow, ctx, now_fn)
        outcome = await _admit(
            uow, ctx, meta, precheck, backoff_seconds, now_fn, transit=transit
        )
        if outcome is not None:
            return outcome
        # Flip committed — the ladder proceeds as a publishing intent (the
        # SQL predicates enforce state DB-side; nothing re-reads the snapshot).
        if ctx.dry_run:
            # Dry Run: everything a post does to the workspace — the cap
            # debit above, the rotation, the card — and no provider call.
            return await _confirm_dry_run(
                uow,
                ctx,
                repost_ttl_days_default=repost_ttl_days_default,
                now_fn=now_fn,
            )

    if state == "publishing" and ctx.intent["publish_step"] == "none" and not ctx.ops:
        # Flipped, nothing else done yet — the point a crash between the flip
        # and the dry-run confirm (or a pause landing mid-hold) re-enters.
        # A dry-run job must confirm as a dry run HERE, never fall into the
        # real ladder below; a paused workspace holds here too.
        if ctx.dry_run:
            return await _confirm_dry_run(
                uow,
                ctx,
                repost_ttl_days_default=repost_ttl_days_default,
                now_fn=now_fn,
            )
        if ctx.intent.get("is_paused") and not ctx.intent["cancel_requested"]:
            return await _defer_paused(uow, ctx, now_fn)
    elif state == "publishing" and ctx.dry_run:
        raise ValueError(
            f"intent {ctx.intent_id}: a dry-run job at step"
            f" {ctx.intent['publish_step']!r} — a dry run never climbs the ladder"
        )

    # -- resume protocol: unresolved permits FIRST (`02` §6 step 2) -------------
    pending_publish = ctx.unresolved("publish")
    if pending_publish is not None:
        return await _park(uow, ctx, pending_publish["id"])
    pending_container = ctx.unresolved("container_create")
    if pending_container is not None:
        async with _leased_tx(uow, ctx.job) as session:
            verdict = await provider_ops.resume_unresolved(
                session,
                op={"id": pending_container["id"], "op_kind": "container_create"},
                intent_id=ctx.intent_id,
            )
        if verdict != "repermit":
            raise ValueError(
                f"resume of container permit {pending_container['id']} returned "
                f"{verdict!r} — the container arm of `02` §6 always repermits"
            )
        # An orphaned container is inert — continue this run from the create
        # (the next generation derives from MAX over committed ops rows).

    return await _ladder(
        uow,
        ctx,
        engine=engine,
        meta=meta,
        transit=transit,
        media_fetch=media_fetch,
        backoff_seconds=backoff_seconds,
        repost_ttl_days_default=repost_ttl_days_default,
        now_fn=now_fn,
        sleep=sleep,
    )


async def _load(uow, job: dict) -> Optional[_Ctx]:
    payload = job["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    intent_id = payload.get("intent_id")
    async with _leased_tx(uow, job) as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT i.id, i.state, i.publish_step, i.cancel_requested,"
                        "       i.media_item_id, i.ig_account_id, i.ig_container_id,"
                        "       i.transit_asset_ref, i.cap_consumed_on, i.attempts_by_step,"
                        "       a.provider_account_ref, i.workspace_id,"
                        "       w.is_paused,"
                        "       m.source_id, m.mime_type,"
                        "       COALESCE(a.posts_per_day, w.posts_per_day) AS eff_ppd,"
                        "       COALESCE(a.tz, w.tz) AS eff_tz,"
                        "       a.next_slot_at, w.repost_ttl_days,"
                        "       m.media_kind, m.provider_file_ref, m.file_name"
                        "  FROM post_intents i"
                        "  JOIN ig_accounts a ON a.id = i.ig_account_id"
                        "  JOIN workspaces w ON w.id = i.workspace_id"
                        "  JOIN media_items m ON m.id = i.media_item_id"
                        " WHERE i.id = :intent"
                    ),
                    {"intent": str(intent_id)},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            logger.error(
                "publish_pipeline job %s: intent %s not found", job["id"], intent_id
            )
            return None
        ops = (
            (
                await session.execute(
                    text(
                        "SELECT id, op_kind, state, generation, business_key,"
                        "       response_ref"
                        "  FROM provider_operations WHERE intent_id = :intent"
                    ),
                    {"intent": str(intent_id)},
                )
            )
            .mappings()
            .all()
        )
    ctx = _Ctx(job, dict(row), [dict(o) for o in ops])
    # The dry-run decision is the JOB's, snapshotted by `approve` when the
    # tapper was told "dry run" — never the workspace's live flag, which may
    # have moved since (a rehearsal that posts for real, or the reverse).
    ctx.dry_run = bool(payload.get("dry_run"))
    return ctx


def _local_date(ctx: _Ctx, now_fn) -> date:
    """The §4 debit day, in the account's effective tz — service-side, with
    the §0 fn_safe_tz rule applied at this site: an unresolvable zone
    degrades to UTC rather than aborting."""
    tz_name = ctx.tz
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 — the §0 rule IS "degrade, never abort"
        tz = timezone.utc
    return now_fn().astimezone(tz).date()


def _slot_at(ctx: _Ctx, now_fn) -> Optional[datetime]:
    """The account's next product slot when the clock has stamped one that
    is still ahead; None when it has not (the caller waits one fallback rung
    and says nothing on the card — a wait of a minute is not a wait the user
    should know about; adversarial review of #1306)."""
    slot = ctx.intent["next_slot_at"]
    if slot is not None and slot > now_fn():
        return slot
    return None


def _next_slot(
    ctx: _Ctx, now_fn, backoff_seconds
) -> tuple[Optional[datetime], datetime]:
    """The deferral target, as `(slot, run_at)`.

    `slot` is the account's next product slot when the clock has stamped one
    and None otherwise — the three callers each branch their card line on it,
    which is why this returns the pair rather than the target alone. `run_at`
    is that slot, or one backoff rung. Written once because it was inlined at
    three sites before anyone called the helper (#1325 audit, TD-A6).
    """
    slot = _slot_at(ctx, now_fn)
    return slot, slot or now_fn() + timedelta(seconds=backoff_seconds[0])


async def _audit_deferral(
    session, ctx: _Ctx, *, reason: str, next_run_at: datetime, state: str
) -> None:
    """The §4 'cap_deferred' audit row — the first direct (non-trigger) audit
    write in the codebase, so it reads the actor GUCs exactly the way the
    triggers do rather than trusting parameters (`audit.ACTOR_FROM_GUCS`)."""
    await audit.record(
        session,
        workspace_id=ctx.workspace_id,
        entity_kind="post_intent",
        entity_id_sql=":intent",
        from_state=state,
        to_state=state,
        intent=ctx.intent_id,
        detail={
            "v": 1,
            "event": "cap_deferred",
            "reason": reason,
            "next_run_at": next_run_at.isoformat(),
        },
    )


async def _admit(
    uow, ctx: _Ctx, meta, precheck, backoff_seconds, now_fn, transit=None
) -> Optional[str]:
    """The admission gate for an `approved` intent: cancel honor, the §8
    advisory pre-check, then the §4 flip. Returns an outcome to stop with,
    or None to continue into the ladder.

    Those three are the three helpers below it: the docstring already named
    the seams, and now the code is cut along them (the tech-debt audit,
    2026-09-20). Each stage still owns its own transaction — none was moved
    across a commit boundary, and the pre-check's provider read stays outside
    every one of them."""
    if ctx.intent["cancel_requested"]:
        return await _honour_cancel(uow, ctx, transit)
    stop = await _advisory_precheck(uow, ctx, meta, precheck, backoff_seconds, now_fn)
    if stop is not None:
        return stop
    return await _flip_to_publishing(uow, ctx, backoff_seconds, now_fn, transit)


async def _advisory_precheck(
    uow, ctx: _Ctx, meta, precheck, backoff_seconds, now_fn
) -> Optional[str]:
    """The `02` §8 advisory pre-check, immediately before the §4 flip:
    Meta's own answer about the account's remaining quota. Returns
    `DEFERRED_META_CAP` to stop with, or None to go on to the flip."""
    # A story re-entering from a wait (the float, plan 03 D3) carries its
    # debit; the advisory pre-check would send it to the next slot with an
    # asset the sweep reaps — it was admitted once, and that answer stands.
    if (
        precheck is not None
        and not ctx.dry_run
        and ctx.intent.get("cap_consumed_on") is None
    ):
        # A provider read — deliberately OUTSIDE any transaction (`02` §8:
        # "immediately before the §4 flip transaction"). Never for a dry run:
        # nothing reaches Instagram in a rehearsal, and Meta's cap must not
        # hold one that spends none of it (adversarial review of #1299).
        verdict = await precheck.check(meta, ctx.intent["provider_account_ref"])
        if verdict == DEFER:
            slot, run_at = _next_slot(ctx, now_fn, backoff_seconds)
            async with uow.begin() as session:
                await reschedule_job(
                    session,
                    ctx.job["id"],
                    ctx.job["lease_token"],
                    run_at=run_at,
                    restore_attempt=True,
                )
                await _audit_deferral(
                    session,
                    ctx,
                    reason="meta_advisory",
                    next_run_at=run_at,
                    state="approved",
                )
                if slot is not None:
                    # Meta's quota is a wait of hours like the cap's: said on
                    # the card (plan 03, UX principle 1).
                    await _say_waiting(session, ctx, next_run_at=slot)
            return DEFERRED_META_CAP
    return None


async def _flip_to_publishing(
    uow, ctx: _Ctx, backoff_seconds, now_fn, transit
) -> Optional[str]:
    """The `02` §4 flip — `approved → publishing` with the cap debit and key
    4 — and the routing of every answer it can give: proceed (None, into the
    ladder), a cancel the flip itself refused, or a deferral (busy, cap) that
    reschedules and says so. `IntentNotApproved` is the race that moved the
    intent while we held the job; the UoW rolled back and the route is
    whatever it became."""
    try:
        async with _leased_tx(uow, ctx.job) as session:
            flip = await publish_cap.flip_to_publishing(
                session,
                intent_id=ctx.intent_id,
                workspace_id=ctx.workspace_id,
                ig_account_id=str(ctx.intent["ig_account_id"]),
                local_date=_local_date(ctx, now_fn),
                effective_cap=int(ctx.intent["eff_ppd"]),
            )
            if flip is FlipOutcome.PROCEED:
                deferred = None
                if ctx.count("busy_waits"):
                    await _bump(session, ctx, busy_waits=0)
            elif flip is FlipOutcome.CANCELLED:
                # The flip refused a cancel that landed after `_load`'s
                # snapshot (plan 03 D3): the same honor as the snapshot's,
                # in this transaction.
                deferred = CANCELLED
                await _cancel_in(session, ctx)
            else:
                busy = flip is FlipOutcome.BUSY
                slot = None
                if busy:
                    waits = ctx.count("busy_waits")
                    seconds = BUSY_RETRY_SECONDS[
                        min(waits, len(BUSY_RETRY_SECONDS) - 1)
                    ]
                    run_at = now_fn() + timedelta(seconds=seconds)
                    await _bump(session, ctx, busy_waits=waits + 1)
                else:
                    slot, run_at = _next_slot(ctx, now_fn, backoff_seconds)
                await reschedule_job(
                    session,
                    ctx.job["id"],
                    ctx.job["lease_token"],
                    run_at=run_at,
                    restore_attempt=True,
                )
                await _audit_deferral(
                    session,
                    ctx,
                    reason="exclusive" if busy else "cap",
                    next_run_at=run_at,
                    state="approved",
                )
                if slot is not None:
                    # A wait of hours is said on the card; a wait of seconds —
                    # the busy re-check, the one-minute fallback when no slot
                    # is stamped — is not (plan 03, UX principle 1). The day's
                    # count is at its cap, so a slot later today cannot post:
                    # the line promises tomorrow.
                    await _say_waiting(session, ctx, next_run_at=slot, day_spent=True)
                deferred = DEFERRED_BUSY if busy else DEFERRED_CAP
    except IntentNotApproved:
        # (1,0): a race moved the intent while we held the job. The UoW
        # rolled back (debit included). Route on what it became.
        cancelled_here = False
        async with _leased_tx(uow, ctx.job) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT state, cancel_requested FROM post_intents"
                        " WHERE id = :intent"
                    ),
                    {"intent": ctx.intent_id},
                )
            ).one()
            if row.state in intent_ledger.TERMINAL_STATES:
                await finalize_job(
                    session, ctx.job["id"], ctx.job["lease_token"], "cancelled"
                )
            elif row.state == "approved" and row.cancel_requested:
                # The cancel landed inside the flip's window (structural
                # review of #1306): the flip's own WHERE refused it and the
                # debit rolled back. The route is the cancel's — the refund
                # and the destroy a stepped-back story carries — never a
                # crash rung.
                await _cancel_in(session, ctx)
                cancelled_here = True
            else:
                raise
        if cancelled_here:
            await _destroy_after_cancel(ctx, transit)
        return CANCELLED
    if deferred == CANCELLED:
        await _destroy_after_cancel(ctx, transit)
    return deferred


async def _cancel_in(session, ctx: _Ctx) -> None:
    """`approved → cancelled` in the caller's transaction, with the refund a
    stepped-back story carries taken FIRST (the terminal freeze forbids a
    refund after the flip; plan 03 D3) and the job finalized."""
    if ctx.intent.get("cap_consumed_on") is not None:
        await publish_cap.refund_cap(
            session,
            intent_id=ctx.intent_id,
            workspace_id=ctx.workspace_id,
            ig_account_id=str(ctx.intent["ig_account_id"]),
        )
    cancelled = (
        await session.execute(
            text(
                "UPDATE post_intents SET state = 'cancelled'"
                " WHERE id = :intent AND state = 'approved' RETURNING id"
            ),
            {"intent": ctx.intent_id},
        )
    ).fetchone()
    if cancelled is None:
        raise ValueError(f"intent {ctx.intent_id} left 'approved' during cancel honor")
    await finalize_job(session, ctx.job["id"], ctx.job["lease_token"], "cancelled")


async def _destroy_after_cancel(ctx: _Ctx, transit) -> None:
    """After the cancel committed: the transit asset a stepped-back story
    carries is destroyed best-effort (FC-3.5); the FC-3.6 sweep is the
    guarantee. Never inside the transaction — a provider call."""
    ref = ctx.intent.get("transit_asset_ref")
    if not ref or transit is None:
        return
    try:
        await transit.destroy(ref, media_kind=ctx.intent["media_kind"])
    except Exception:  # noqa: BLE001 — the sweep owns what this misses
        logger.warning(
            "publish_pipeline intent %s: transit destroy after cancel failed;"
            " the sweep will reap it",
            ctx.intent_id,
        )


async def _honour_cancel(uow, ctx: _Ctx, transit) -> str:
    """The snapshot's cancel (`cancel_requested` seen at `_load`)."""
    async with _leased_tx(uow, ctx.job) as session:
        await _cancel_in(session, ctx)
    await _destroy_after_cancel(ctx, transit)
    return CANCELLED


async def _park(uow, ctx: _Ctx, op_id) -> str:
    """`02` §6: an unresolved publish permit is NEVER re-called. Park."""
    async with _leased_tx(uow, ctx.job) as session:
        verdict = await provider_ops.resume_unresolved(
            session,
            op={"id": op_id, "op_kind": "publish"},
            intent_id=ctx.intent_id,
        )
        await finalize_job(session, ctx.job["id"], ctx.job["lease_token"], "succeeded")
    if verdict != "parked":
        raise ValueError(
            f"resume of publish permit {op_id} returned {verdict!r} — the "
            "publish arm of `02` §6 always parks"
        )
    return PARKED_AMBIGUOUS


@dataclass(frozen=True)
class _Wait:
    """How a float is audited: its class, its rung index and the story's
    counters (plan 03 D3). Five parameters travelled together through the
    thirteen call sites of `_retry_or_poison` the audit counted — ten now
    that the two readiness rungs share `_route_readiness` — and one object
    is the same data with one name (the tech-debt audit, 2026-09-20).

    *kind* is the class recorded on the wait (today's `wait=` string).
    *attempt* is the count that indexes the rung (a class's own, e.g.
    `fetch_waits`; the job's attempts when None), *spent* says the class's
    ladder is exhausted (the job's budget when None), *counters* merge into
    `attempts_by_step`, and *restore_attempt* keeps the job's attempt for the
    classes whose bound is their own ladder.
    """

    kind: str = "retry"
    attempt: Optional[int] = None
    spent: Optional[bool] = None
    counters: Optional[dict] = None
    restore_attempt: bool = False


async def _retry_or_poison(
    uow,
    ctx: _Ctx,
    backoff_seconds,
    now_fn,
    *,
    resolve_op_id=None,
    resolve_response: Optional[dict] = None,
    step_back_to: Optional[str] = None,
    error: Optional[dict] = None,
    poison_now: bool = False,
    wait: _Wait = _Wait(),
) -> str:
    """R8's retryable-failure edge: reschedule on the ladder while the budget
    holds; G5 poison (`publishing → review_required`, debit retained) when it
    is spent. *resolve_op_id* records a definitive failure on the permit in
    the same transaction; *step_back_to* rewinds the ladder (the dead- or
    gone-container case).

    The float (plan 03): every wait STEPS BACK — `publishing → approved` in
    this same transaction, progress and debit kept, so the account's slot is
    free while the story waits — and is audited with its class, its rung and
    the story's counters. That description is *wait*, a `_Wait`; the outcome
    keywords above it say what happened, which is a different thing and stays
    spelled out.
    """
    attempts = int(ctx.job["attempts"])
    # *poison_now*: the failure cannot be retried into success (a dead
    # credential) — skip the ladder, hand the intent to a human at once.
    spent = wait.spent
    if spent is None:
        spent = attempts >= int(ctx.job["max_attempts"])
    exhausted = poison_now or spent
    now = now_fn()
    async with _leased_tx(uow, ctx.job) as session:
        if error is not None:
            # The reason, on the row, for whichever surface reads it next —
            # the operator's review queue or the customer notice. Written
            # before the flip; `review_required` is not terminal-frozen.
            await session.execute(
                text(
                    "UPDATE post_intents SET last_error = CAST(:e AS jsonb) WHERE id = :intent"
                ),
                {"e": json.dumps(error), "intent": ctx.intent_id},
            )
        if resolve_op_id is not None:
            await provider_ops.resolve_permit(
                session,
                op_id=resolve_op_id,
                outcome="failed",
                response_ref=resolve_response,
            )
        if wait.counters:
            await _bump(session, ctx, **wait.counters)
        # `step_back_to` is the caller saying the artifacts BEYOND that rung are
        # abandoned — the dead- and gone-container paths. So it is also the
        # signal for whether `ig_container_id` still points at anything, and
        # it is the right discriminator on BOTH branches below.
        #
        # It must NOT be a blanket clear on poison. Poison is shared by every
        # retryable class: a poison after a failed PUBLISH leaves a container
        # that is still live and reusable, and nulling its id there would throw
        # away a valid pointer. Only a caller that rewound past the container
        # rung is telling us the container is gone (#938).
        drop_container = ", ig_container_id = NULL" if step_back_to else ""
        # A step back to `none` is the caller saying the UPLOAD is abandoned
        # too (the swept asset): the ref goes with the container, and the next
        # run uploads afresh.
        if step_back_to == "none":
            drop_container += ", transit_asset_ref = NULL"
        if exhausted:
            moved = (
                await session.execute(
                    text(
                        "UPDATE post_intents SET state = 'review_required'"
                        + drop_container
                        + " WHERE id = :intent AND state = 'publishing' RETURNING id"
                    ),
                    {"intent": ctx.intent_id},
                )
            ).fetchone()
            if moved is None:
                raise ValueError(
                    f"intent {ctx.intent_id} left 'publishing' during poison"
                )
            # Said at once, not six hours later: the card gains the review
            # line and the workspace hears; the reconciler's own notice reads
            # the latch and stays silent (investigation of 2026-09-11).
            told = await _say_outcome(
                session,
                ctx,
                state="review_required",
                at=now,
                notice=_poison_notice(ctx, now),
                # The card keeps buttons: the workspace resolves its own
                # review (2026-09-12) — post again, it posted, give up.
                reply_markup=prompts.review_keyboard(ctx.intent_id),
            )
            if told:
                # The latch is the reconciler's own rule (`reconciler.py`): it is
                # stamped only when an audience heard, so a workspace with no
                # push binding at poison time keeps its six-hour backstop.
                await session.execute(
                    text(
                        "UPDATE post_intents SET "
                        + intent_ledger.EVIDENCE_MERGE.format(
                            seed="{}", key="customer_notified", value="true"
                        )
                        + " WHERE id = :intent"
                    ),
                    {"intent": ctx.intent_id},
                )
            await finalize_job(
                session, ctx.job["id"], ctx.job["lease_token"], "review_required"
            )
            return POISONED
        if step_back_to is not None:
            await session.execute(
                text(
                    "UPDATE post_intents SET publish_step = :step"
                    + drop_container
                    + " WHERE id = :intent AND state = 'publishing'"
                ),
                {"step": step_back_to, "intent": ctx.intent_id},
            )
        index = (wait.attempt if wait.attempt is not None else attempts) - 1
        rung = backoff_seconds[max(0, min(index, len(backoff_seconds) - 1))]
        run_at = now + timedelta(seconds=rung)
        await reschedule_job(
            session,
            ctx.job["id"],
            ctx.job["lease_token"],
            run_at=run_at,
            restore_attempt=wait.restore_attempt,
        )
        # The wait itself (plan 03 D3): out of the slot, on the record.
        await _step_back(session, ctx)
        if not ctx.counters().get("float_since"):
            await _bump(session, ctx, float_since=now.isoformat())
        await _audit_wait(
            session,
            ctx,
            wait=wait.kind,
            rung=index + 1,
            seconds=rung,
            next_run_at=run_at,
        )
    return RETRY_SCHEDULED


def _poison_notice(ctx: _Ctx, now: datetime) -> str:
    """The one sentence the workspace hears when a story parks (plan 03 D6):
    how long Instagram was tried when the float's start is on the row."""
    since = ctx.counters().get("float_since")
    if since:
        try:
            started = datetime.fromisoformat(str(since))
            minutes = max(1, int((now - started).total_seconds() // 60))
            return (
                f"A story couldn't be posted after {minutes} minutes of trying and"
                " needs attention: choose on its card, or open the Queue on the web."
            )
        except (ValueError, TypeError):
            pass
    return (
        "A story couldn't be posted after several tries and needs"
        " attention: choose on its card, or open the Queue on the web."
    )


async def _bump(session, ctx: _Ctx, **changes) -> dict:
    """Merge *changes* into the story's `attempts_by_step`, on the row (`||`,
    never a wholesale write from this copy, so a key another writer adds
    while the job holds the story survives — adversarial review of #1306)
    and on the context."""
    merged = {**ctx.counters(), **changes}
    await session.execute(
        text(
            "UPDATE post_intents SET attempts_by_step ="
            " COALESCE(attempts_by_step, '{}'::jsonb) || CAST(:c AS jsonb)"
            " WHERE id = :intent"
        ),
        {"c": json.dumps(changes), "intent": ctx.intent_id},
    )
    ctx.intent["attempts_by_step"] = merged
    return merged


async def _step_back(session, ctx: _Ctx) -> None:
    """`publishing → approved` (edge 076): the story waits outside the
    account's slot with its progress and its debit intact. A zero-row update
    is a race the caller must hear about, as every checkpoint's is."""
    moved = (
        await session.execute(
            text(
                "UPDATE post_intents SET state = 'approved'"
                " WHERE id = :intent AND state = 'publishing' RETURNING id"
            ),
            {"intent": ctx.intent_id},
        )
    ).fetchone()
    if moved is None:
        raise ValueError(f"intent {ctx.intent_id} left 'publishing' during a wait")
    ctx.intent["state"] = "approved"


async def _audit_wait(
    session, ctx: _Ctx, *, wait: str, rung: int, seconds: float, next_run_at: datetime
) -> None:
    """The wait's own audit row (plan 03 D3): the trigger's `publishing →
    approved` row carries no detail, so the class, the rung and the story's
    counters ride a direct row in `_audit_deferral`'s shape."""
    await audit.record(
        session,
        workspace_id=ctx.workspace_id,
        entity_kind="post_intent",
        entity_id_sql=":intent",
        from_state="approved",
        to_state="approved",
        intent=ctx.intent_id,
        detail={
            "v": 1,
            "event": "float_wait",
            "class": wait,
            "rung": rung,
            "seconds": seconds,
            "next_run_at": next_run_at.isoformat(),
            "counters": ctx.counters(),
        },
    )


async def _say_waiting(
    session, ctx: _Ctx, *, next_run_at: datetime, day_spent: bool = False
) -> None:
    """A wait the user should know about — hours, a cap — is said on the
    card's line ("✅ Approved · posts tomorrow 09:00"); no notice (plan 03,
    UX principle 1). A wait of seconds says nothing. *day_spent* is the
    local cap's case: a slot later today cannot post, so the line promises
    tomorrow."""
    line = prompts.waiting_line(
        next_run_at,
        tz=ctx.tz,
        now=datetime.now(timezone.utc),
        day_spent=day_spent,
    )
    await outbox.restate_everywhere(
        session,
        workspace_id=ctx.workspace_id,
        intent_id=ctx.intent_id,
        outcome_text=line,
    )


async def _permit(engine, ctx: _Ctx, *, op_kind: str, generation: int) -> dict:
    """The §6 permit transaction on its OWN connection: tenant + actor GUCs
    applied transaction-locally, then `acquire_permit` (which owns the commit).
    Never inside a UoW — the permit tx must commit before the provider call,
    and `acquire_permit` commits the connection it is handed."""
    async with engine.connect() as conn:
        await apply_gucs(conn, tenant_id=ctx.workspace_id, actor_kind="system")
        return await provider_ops.acquire_permit(
            conn,
            workspace_id=ctx.workspace_id,
            intent_id=ctx.intent_id,
            job_id=ctx.job["id"],
            lease_token=ctx.job["lease_token"],
            op_kind=op_kind,
            generation=generation,
        )


async def _route_readiness(
    uow, ctx: _Ctx, meta, sleep, backoff_seconds, now_fn
) -> Optional[str]:
    """One bounded readiness segment, routed. Returns the outcome to stop
    with, or None when the container is ready and the caller continues.

    Both rungs that poll a container — `container_created` and
    `publish_called` — routed the verdict through identical code, the
    `ContainerDead` error dict included (the tech-debt audit, 2026-09-20).
    A verdict that means one thing on one rung and another on the next is
    exactly the drift a second copy produces, so the routing lives once.

    No transaction is open across the poll: `_await_ready` is the provider
    call and every branch below opens its own (`02` §5).
    """
    verdict = await _await_ready(ctx, meta, sleep)
    if verdict == "dead":
        # The container is definitively gone; a NEW one needs a fresh
        # create (and a fresh generation) from the still-valid transit
        # asset — step back, retry on the ladder.
        return await _retry_or_poison(
            uow,
            ctx,
            backoff_seconds,
            now_fn,
            step_back_to="transit_uploaded",
            error={
                "v": 1,
                "error": {
                    "type": "ContainerDead",
                    "code": None,
                    "message": "Meta reported the container ERROR or EXPIRED —"
                    " the media at the delivery URL could not be processed",
                },
            },
        )
    if verdict == "unauthorized":
        return await _retry_or_poison(
            uow,
            ctx,
            backoff_seconds,
            now_fn,
            error=_error_of(ctx.poll_error),
            poison_now=True,
        )
    if verdict == "pending":
        return await _retry_or_poison(uow, ctx, backoff_seconds, now_fn)
    return None


async def _ladder(
    uow,
    ctx: _Ctx,
    *,
    engine,
    meta,
    transit,
    media_fetch,
    backoff_seconds,
    repost_ttl_days_default,
    now_fn,
    sleep,
) -> str:
    """The checkpoint ladder for a `publishing` intent, entered at
    ``publish_step`` and driven forward one checkpoint at a time."""
    step = ctx.intent["publish_step"]

    if step == "none":
        # The fetch and the upload are provider calls too (Drive, Cloudinary),
        # and a typed failure out of this rung would otherwise reach the
        # loop's blanket reschedule — every minute, forever, the intent stuck
        # in `publishing` with its cap debit and `uq_publish_exclusive` held,
        # so every later intent on the account is deferred (#1276 review). A
        # file that is gone or too large is terminal: fail + refund. Every
        # other TYPED failure (Drive's, the transit store's, the floor's, the
        # transport's) rides the ladder and, exhausted, lands on a human. An
        # untyped exception still propagates: a crash must look like a crash.
        try:
            media = await media_fetch(dict(ctx.intent))
            ref = await transit.upload(
                media,
                workspace_id=ctx.workspace_id,
                media_kind=ctx.intent["media_kind"],
            )
        except DriveTerminalError as exc:
            logger.warning(
                "publish_pipeline intent %s: media unavailable (%s) — failing",
                ctx.intent_id,
                type(exc).__name__,
            )
            return await _fail_terminal(uow, ctx, op_id=None, exc=exc, now_fn=now_fn)
        except (DriveError, DriveLostResponse, StorydumpError, httpx.HTTPError) as exc:
            logger.warning(
                "publish_pipeline intent %s: fetch/upload failed (%s) — retrying",
                ctx.intent_id,
                type(exc).__name__,
            )
            return await _retry_or_poison(
                uow, ctx, backoff_seconds, now_fn, error=_error_of(exc)
            )
        async with _leased_tx(uow, ctx.job) as session:
            advanced = (
                await session.execute(
                    text(
                        "UPDATE post_intents SET transit_asset_ref = :ref,"
                        " publish_step = 'transit_uploaded'"
                        " WHERE id = :intent AND state = 'publishing' RETURNING id"
                    ),
                    {"ref": ref, "intent": ctx.intent_id},
                )
            ).fetchone()
        if advanced is None:
            raise ValueError(f"intent {ctx.intent_id} left 'publishing' mid-ladder")
        ctx.intent["transit_asset_ref"] = ref
        step = "transit_uploaded"

    if step == "transit_uploaded":
        # The fresh-url round (plan 03 D1): a refused fetch is retried AT ONCE
        # with a url Meta has never seen for this story — variant n, where n is
        # the story's refusal count, so no round re-offers a refused url — up
        # to FRESH_URLS_PER_ROUND per run; then the story steps back and waits.
        # A fresh url for a VIDEO is a fresh encode — tens of seconds, not the
        # probe's budget — so a refused video offers no fresh urls: it steps
        # back at once and offers the same url after the wait, accepted at
        # +30 s in 10 of 17 trials (`02`, the evening; plan 03 D1 as amended
        # 2026-09-14).
        fresh_budget = (
            FRESH_URLS_PER_ROUND if ctx.intent["media_kind"] == "image" else 0
        )
        fresh_this_run = 0
        while True:
            variant = ctx.count("fetch_refusals") if fresh_budget else 0
            # The frame must SERVE before Meta is told to fetch it
            # (investigation of 2026-09-11: Meta's fetch arrives within a
            # second of the container call, and a frame not yet serving is a
            # lost post). A transit store without a readiness probe (an older
            # seam) is taken as ready.
            ready = getattr(transit, "ready", None)
            readiness = (
                await ready(
                    ctx.intent["transit_asset_ref"],
                    media_kind=ctx.intent["media_kind"],
                    sleep=sleep,
                    variant=variant,
                )  # the budget is the store's, per media kind
                if ready is not None
                else None
            )
            # What the worker itself saw of the frame, kept beside Meta's
            # answer on the permit (2026-09-13).
            probe = getattr(readiness, "observation", None)
            if ready is not None and not readiness:
                logger.warning(
                    "publish_pipeline intent %s: the story frame is not serving yet —"
                    " retrying on the ladder rather than handing Meta a dead URL"
                    " (the probe saw %s)",
                    ctx.intent_id,
                    probe,
                )
                not_ready = _error_of(
                    TransitNotReady("story frame not serving within budget")
                )
                not_ready["error"]["probe"] = probe
                # A 404 after the probe's budget is an asset the CDN does not
                # know — swept while the story waited past the transit TTL (a
                # weekend without slots, a long pause; adversarial review of
                # #1306) — so the story steps back to step `none` without its
                # ref and the next run uploads again. Any other answer is
                # "not yet": the asset and the step stay.
                gone = bool(probe) and probe.get("status") in (404, 410)
                return await _retry_or_poison(
                    uow,
                    ctx,
                    backoff_seconds,
                    now_fn,
                    error=not_ready,
                    step_back_to="none" if gone else None,
                )
            generation = ctx.next_generation("container_create")
            permit = await _permit(
                engine, ctx, op_kind="container_create", generation=generation
            )
            # The next fresh url's permit must see this one (plan 03 D5):
            # `next_generation` reads `ctx.ops`, loaded once.
            ctx.ops.append(
                {
                    "id": permit["id"],
                    "op_kind": "container_create",
                    "generation": generation,
                    "state": "permitted",
                }
            )
            media_url = transit.delivery_url(
                ctx.intent["transit_asset_ref"],
                media_kind=ctx.intent["media_kind"],
                variant=variant,
            )
            started = time.perf_counter()
            try:
                container_id = await meta.create_container(
                    ctx.intent["provider_account_ref"],
                    media_url=media_url,
                    media_kind=ctx.intent["media_kind"],
                    workspace_id=ctx.workspace_id,
                )
            except MetaTerminalError as exc:
                if exc.code == FETCH_FAILED_CODE:
                    elapsed_ms = ms_since(started)
                    refusals = ctx.count("fetch_refusals") + 1
                    record = _permit_record(
                        {
                            "v": 1,
                            "error": exc.code,
                            "fetch_failed": True,
                            "url_variant": variant,
                        },
                        exc=exc,
                        elapsed_ms=elapsed_ms,
                        probe=probe,
                    )
                    if fresh_this_run < fresh_budget:
                        fresh_this_run += 1
                        logger.warning(
                            "publish_pipeline intent %s: Meta could not fetch the frame"
                            " (code %s/%s, trace %s, %d ms; the probe saw %s) — a fresh"
                            " url at once (%d of %d this round)",
                            ctx.intent_id,
                            exc.code,
                            exc.subcode,
                            exc.detail.get("fbtrace_id"),
                            elapsed_ms,
                            probe,
                            fresh_this_run,
                            fresh_budget,
                        )
                        async with _leased_tx(uow, ctx.job) as session:
                            await provider_ops.resolve_permit(
                                session,
                                op_id=permit["id"],
                                outcome="failed",
                                response_ref=record,
                            )
                            await session.execute(
                                text(
                                    "UPDATE post_intents SET last_error = CAST(:e AS jsonb)"
                                    " WHERE id = :intent"
                                ),
                                {
                                    "e": json.dumps(_error_of(exc)),
                                    "intent": ctx.intent_id,
                                },
                            )
                            await _bump(session, ctx, fetch_refusals=refusals)
                        ctx.ops[-1]["state"] = "failed"
                        continue
                    waits = ctx.count("fetch_waits") + 1
                    logger.warning(
                        "publish_pipeline intent %s: Meta could not fetch the frame"
                        " (code %s/%s, trace %s, %d ms) — the round is spent; wait %d",
                        ctx.intent_id,
                        exc.code,
                        exc.subcode,
                        exc.detail.get("fbtrace_id"),
                        elapsed_ms,
                        waits,
                    )
                    return await _retry_or_poison(
                        uow,
                        ctx,
                        FETCH_RETRY_SECONDS,
                        now_fn,
                        resolve_op_id=permit["id"],
                        resolve_response=record,
                        error=_error_of(exc),
                        wait=_Wait(
                            kind="fetch",
                            attempt=waits,
                            spent=waits > len(FETCH_RETRY_SECONDS),
                            counters={
                                "fetch_refusals": refusals,
                                "fetch_waits": min(waits, len(FETCH_RETRY_SECONDS)),
                            },
                            restore_attempt=True,
                        ),
                    )
                return await _fail_terminal(
                    uow,
                    ctx,
                    op_id=permit["id"],
                    exc=exc,
                    now_fn=now_fn,
                    record=_permit_record(
                        {"url_variant": variant},
                        exc=exc,
                        elapsed_ms=ms_since(started),
                        probe=probe,
                    ),
                )
            except MetaError as exc:
                logger.warning(
                    "publish_pipeline intent %s: %s code=%s — %s",
                    ctx.intent_id,
                    type(exc).__name__,
                    exc.code,
                    "handing to a human" if _dead_credential(exc) else "retrying",
                )
                return await _retry_or_poison(
                    uow,
                    ctx,
                    backoff_seconds,
                    now_fn,
                    resolve_op_id=permit["id"],
                    resolve_response=_permit_record(
                        {"v": 1, "error": exc.code, "url_variant": variant},
                        exc=exc,
                        elapsed_ms=ms_since(started),
                        probe=probe,
                    ),
                    error=_error_of(exc),
                    poison_now=_dead_credential(exc),
                )
            except MetaLostResponse:
                # Lost response on a RECOVERABLE effect (`02` §6): resolve it
                # the way a crash-resume would — failed/lost_response — and
                # retry on the ladder. Never intent-level ambiguity for a
                # container. (Anything untyped propagates: a crash must look
                # like a crash.)
                return await _retry_or_poison(
                    uow,
                    ctx,
                    backoff_seconds,
                    now_fn,
                    resolve_op_id=permit["id"],
                    resolve_response=_permit_record(
                        {"v": 1, "error": "lost_response", "url_variant": variant},
                        elapsed_ms=ms_since(started),
                        probe=probe,
                    ),
                )
            elapsed_ms = ms_since(started)
            async with _leased_tx(uow, ctx.job) as session:
                await provider_ops.resolve_permit(
                    session,
                    op_id=permit["id"],
                    outcome="succeeded",
                    response_ref=_permit_record(
                        {"v": 1, "container_id": container_id, "url_variant": variant},
                        elapsed_ms=elapsed_ms,
                        probe=probe,
                    ),
                )
                advanced = (
                    await session.execute(
                        text(
                            "UPDATE post_intents SET ig_container_id = :cid,"
                            " publish_step = 'container_created'"
                            " WHERE id = :intent AND state = 'publishing' RETURNING id"
                        ),
                        {"cid": container_id, "intent": ctx.intent_id},
                    )
                ).fetchone()
            if advanced is None:
                raise ValueError(f"intent {ctx.intent_id} left 'publishing' mid-ladder")
            ctx.ops[-1]["state"] = "succeeded"
            ctx.intent["ig_container_id"] = container_id
            step = "container_created"
            break

    if step == "container_created":
        stop = await _route_readiness(uow, ctx, meta, sleep, backoff_seconds, now_fn)
        if stop is not None:
            return stop
        async with _leased_tx(uow, ctx.job) as session:
            await session.execute(
                text(
                    "UPDATE post_intents SET publish_step = 'container_ready'"
                    " WHERE id = :intent AND state = 'publishing'"
                ),
                {"intent": ctx.intent_id},
            )
        step = "container_ready"

    if step == "publish_called":
        # Resume behind a RESOLVED publish permit (an unresolved one parked
        # above). `failed` = a definitive non-effect (error 9 / retryable):
        # re-arm through the readiness check so a dead container is caught
        # before a fresh permit. `succeeded` here is unreachable — the same
        # transaction that records success advances the step.
        latest = ctx.latest("publish")
        if latest is not None and latest["state"] == "succeeded":
            raise ValueError(
                f"intent {ctx.intent_id}: publish permit succeeded but step "
                "is still publish_called — the terminal tx writes both"
            )
        stop = await _route_readiness(uow, ctx, meta, sleep, backoff_seconds, now_fn)
        if stop is not None:
            return stop
        step = "container_ready"

    if step == "container_ready":
        generation = ctx.next_generation("publish")
        permit = await _permit(engine, ctx, op_kind="publish", generation=generation)
        try:
            media_id = await meta.publish(
                ctx.intent["provider_account_ref"],
                ctx.intent["ig_container_id"],
                workspace_id=ctx.workspace_id,
            )
        except MetaCapDeferral as exc:
            # Error 9 (`02` §8): a cap, not a fault. Definitive non-effect →
            # resolve failed; defer to the next slot; the debit stands.
            slot, run_at = _next_slot(ctx, now_fn, backoff_seconds)
            async with _leased_tx(uow, ctx.job) as session:
                await provider_ops.resolve_permit(
                    session,
                    op_id=permit["id"],
                    outcome="failed",
                    response_ref={
                        "v": 1,
                        "error": exc.code,
                        "reason": "meta_publish_cap",
                    },
                )
                await reschedule_job(
                    session,
                    ctx.job["id"],
                    ctx.job["lease_token"],
                    run_at=run_at,
                    restore_attempt=True,
                )
                # A wait of hours leaves the slot (plan 03: a story never waits
                # inside it); the container is kept. Said on the card when the
                # wait is a slot, not the one-minute fallback.
                await _step_back(session, ctx)
                await _audit_deferral(
                    session,
                    ctx,
                    reason="error_9",
                    next_run_at=run_at,
                    state="approved",
                )
                if slot is not None:
                    await _say_waiting(session, ctx, next_run_at=slot)
            return DEFERRED_META_CAP
        except MetaTerminalError as exc:
            return await _fail_terminal(uow, ctx, op_id=permit["id"], exc=exc)
        except MetaError as exc:
            if exc.code == CONTAINER_GONE_CODE:
                gone = ctx.count("container_gone") + 1
                logger.warning(
                    "publish_pipeline intent %s: Meta cannot find the container it"
                    " reported ready (code %s/%s, trace %s) — recreating it, wait %d",
                    ctx.intent_id,
                    exc.code,
                    exc.subcode,
                    exc.detail.get("fbtrace_id"),
                    gone,
                )
                return await _retry_or_poison(
                    uow,
                    ctx,
                    CONTAINER_GONE_RETRY_SECONDS,
                    now_fn,
                    resolve_op_id=permit["id"],
                    resolve_response=_permit_record(
                        {"v": 1, "error": exc.code, "container_gone": True}, exc=exc
                    ),
                    error=_error_of(exc),
                    step_back_to="transit_uploaded",
                    wait=_Wait(
                        kind="container",
                        attempt=gone,
                        spent=gone > len(CONTAINER_GONE_RETRY_SECONDS),
                        counters={
                            "container_gone": min(
                                gone, len(CONTAINER_GONE_RETRY_SECONDS)
                            )
                        },
                        restore_attempt=True,
                    ),
                )
            logger.warning(
                "publish_pipeline intent %s: %s code=%s — %s",
                ctx.intent_id,
                type(exc).__name__,
                exc.code,
                "handing to a human" if _dead_credential(exc) else "retrying",
            )
            return await _retry_or_poison(
                uow,
                ctx,
                backoff_seconds,
                now_fn,
                resolve_op_id=permit["id"],
                resolve_response=_permit_record({"v": 1, "error": exc.code}, exc=exc),
                error=_error_of(exc),
                poison_now=_dead_credential(exc),
            )
        except MetaLostResponse:
            # Lost response on the IRREVERSIBLE effect: R8. The call may have
            # landed; nobody knows. Park with zero retries; the reconciler
            # owns it from here. (Anything untyped propagates — the resume
            # protocol reaches this same park from the unresolved permit.)
            return await _park(uow, ctx, permit["id"])
        return await _confirm(
            uow,
            ctx,
            permit=permit,
            media_id=media_id,
            transit=transit,
            repost_ttl_days_default=repost_ttl_days_default,
            now_fn=now_fn,
        )

    if step == "effect_confirmed":
        raise ValueError(
            f"intent {ctx.intent_id}: step effect_confirmed with state "
            "publishing — the terminal tx writes both together"
        )
    raise ValueError(f"intent {ctx.intent_id}: unknown publish_step {step!r}")


def _dead_credential(exc: BaseException) -> bool:
    """A missing or dead Instagram token: no retry mints one, so the ladder
    is skipped and the intent goes to a human with the reason on it."""
    return isinstance(exc, MetaRetryableError) and exc.code == OAUTH_ERROR_CODE


def _permit_record(
    base: dict,
    *,
    exc: Optional[BaseException] = None,
    elapsed_ms: Optional[int] = None,
    probe: Optional[dict] = None,
) -> dict:
    """The container permit's `response_ref` (2026-09-13): the outcome, how
    long Meta took to answer, what the worker's own probe had just seen of
    the frame and, on a refusal, Meta's whole answer — so the next refused
    fetch is a ledger query, not a day's forensics over a removed deploy's
    logs."""
    record = dict(base)
    if elapsed_ms is not None:
        record["elapsed_ms"] = elapsed_ms
    if probe is not None:
        record["probe"] = probe
    if exc is not None:
        detail = getattr(exc, "detail", None) or {}
        record["meta"] = {
            "subcode": getattr(exc, "subcode", None),
            "user_title": detail.get("error_user_title"),
            "user_msg": detail.get("error_user_msg"),
            "fbtrace_id": detail.get("fbtrace_id"),
            "http_status": detail.get("http_status"),
        }
    return record


def _error_of(exc: BaseException) -> dict:
    """What `post_intents.last_error` records for a retry or a poison: the
    type, the provider code (and subcode and trace id when Meta gave them),
    and the (already redacted) message — so the operator surface can say
    WHY, not just that."""
    error: dict = {
        "type": type(exc).__name__,
        "code": getattr(exc, "code", None),
        # NUL would make PostgreSQL refuse the jsonb inside the retry's own
        # transaction — a crash where a retry was meant.
        "message": str(exc).replace("\x00", "")[:500],
    }
    subcode = getattr(exc, "subcode", None)
    if subcode is not None:
        error["subcode"] = subcode
    trace = (getattr(exc, "detail", None) or {}).get("fbtrace_id")
    if trace:
        error["fbtrace_id"] = trace
    return {"v": 1, "error": error}


async def _await_ready(ctx: _Ctx, meta, sleep) -> str:
    """One bounded readiness segment: 'ready' | 'dead' | 'unauthorized' |
    'pending'.

    'unauthorized' is the fourth and it is not a variant of 'pending': a dead
    credential is what no rung of the ladder can mend, so it leaves at once
    for the human instead of spending the poll budget (see below).
    """
    for attempt in range(POLL_BUDGET):
        try:
            status = await meta.container_status(
                ctx.intent["ig_container_id"],
                provider_account_ref=ctx.intent["provider_account_ref"],
                workspace_id=ctx.workspace_id,
            )
        except (MetaError, MetaLostResponse) as exc:
            # A poll has no effect to lose: a typed failure here is one more
            # "not ready yet" rung on the attempts ladder, bounded at a human —
            # except a dead credential, which no rung can mend: it goes to the
            # human at once, as it does at the effects.
            if _dead_credential(exc):
                ctx.poll_error = exc
                return "unauthorized"
            logger.warning(
                "publish_pipeline intent %s: readiness poll failed (%s) — pending",
                ctx.intent_id,
                type(exc).__name__,
            )
            return "pending"
        if status in _READY_STATUSES:
            return "ready"
        if status in _DEAD_STATUSES:
            return "dead"
        if attempt + 1 < POLL_BUDGET:
            await sleep(POLL_INTERVAL_S)
    return "pending"


async def _defer_paused(uow, ctx: _Ctx, now_fn) -> str:
    """The workspace is paused: reschedule without spending an attempt. A
    fresh `approved` row carries nothing; a stepped-back one (the float)
    carries its progress and its debit and waits with them; from the
    post-flip hold (step `none`, no permits) the row is `publishing` with its
    debit — it steps back and waits with it, so no wait holds the slot
    (structural review of #1306); the re-entrant flip takes it in again.
    Resume is a fresh run."""
    async with _leased_tx(uow, ctx.job) as session:
        if ctx.intent["state"] == "publishing":
            await _step_back(session, ctx)
        await reschedule_job(
            session,
            ctx.job["id"],
            ctx.job["lease_token"],
            run_at=now_fn() + timedelta(seconds=PAUSE_RECHECK_SECONDS),
            restore_attempt=True,
        )
    logger.info(
        "publish_pipeline intent %s: workspace paused — held %ss",
        ctx.intent_id,
        PAUSE_RECHECK_SECONDS,
    )
    return DEFERRED_PAUSED


async def _confirm_dry_run(
    uow, ctx: _Ctx, *, repost_ttl_days_default: int, now_fn
) -> str:
    """The dry-run terminal transaction: the `posted` row with
    `published_via = 'dry_run'` (no container, no media id from Meta), the
    same rotation effects a real post has (`times_posted`, the recent lock,
    `last_posted_at`), the card restated with the dry-run line, the job
    finalized — one transaction, nothing spoken to any provider."""
    async with uow.begin() as session:
        moved = (
            await session.execute(
                text(
                    "UPDATE post_intents SET state = 'posted', last_error = NULL,"
                    " publish_step = 'effect_confirmed',"
                    " published_via = 'dry_run', ig_media_id = 'dry-run'"
                    " WHERE id = :intent AND state = 'publishing' RETURNING id"
                ),
                {"intent": ctx.intent_id},
            )
        ).fetchone()
        if moved is None:
            raise ValueError(f"intent {ctx.intent_id} left 'publishing' during dry run")
        # `04`'s effect list, in the ledger's one spelling (shared with the
        # manual path and the review card's "it posted").
        await intent_ledger.posted_effects(
            session,
            workspace_id=ctx.workspace_id,
            media_item_id=str(ctx.intent["media_item_id"]),
            ig_account_id=str(ctx.intent["ig_account_id"]),
            intent_id=ctx.intent_id,
            ttl_days=int(ctx.intent["repost_ttl_days"] or repost_ttl_days_default),
        )
        line = prompts.outcome_line(
            "dry_run",
            by=None,
            at=now_fn(),
            tz=ctx.tz,
        )
        await outbox.restate_everywhere(
            session,
            workspace_id=ctx.workspace_id,
            intent_id=ctx.intent_id,
            outcome_text=line,
        )
        await finalize_job(session, ctx.job["id"], ctx.job["lease_token"], "succeeded")
    return POSTED_DRY_RUN


async def _fail_terminal(
    uow,
    ctx: _Ctx,
    *,
    op_id: Optional[str],
    exc: BaseException,
    now_fn=None,
    record: Optional[dict] = None,
) -> str:
    """`publishing → failed` on a definitive permanent failure: permit failed
    (when a permit exists — the fetch rung has none) + state flip with the
    reason + cap refund + job failed, ONE transaction (`02` §4: the refund
    rides the terminal flip's transaction). The card is restated and the
    workspace told in the same transaction (investigation of 2026-09-11: a
    failed post read "Approved" indefinitely and nobody was told). Nothing
    here locks the file: no answer Meta gives about a fetch is the file's
    own (2026-09-12), and a file that is gone or too large is Drive's."""
    at = now_fn() if now_fn is not None else datetime.now(timezone.utc)
    async with _leased_tx(uow, ctx.job) as session:
        if op_id is not None:
            await provider_ops.resolve_permit(
                session,
                op_id=op_id,
                outcome="failed",
                response_ref={
                    # The container rung passes Meta's answer, the call's
                    # duration and the probe's observation (2026-09-13); the
                    # fixed keys come last so the record can never rename
                    # the outcome.
                    **(record or {}),
                    "v": 1,
                    "error": getattr(exc, "code", type(exc).__name__),
                    "terminal": True,
                },
            )
        # Refund BEFORE the flip: the terminal-freeze trigger makes the row
        # immutable the moment state='failed' lands in this transaction, so a
        # refund stamped after it is refused. Same-tx per `02` §4; the ORDER
        # inside the tx is the trigger's to dictate — the gate caught this.
        await publish_cap.refund_cap(
            session,
            intent_id=ctx.intent_id,
            workspace_id=ctx.workspace_id,
            ig_account_id=str(ctx.intent["ig_account_id"]),
        )
        moved = (
            await session.execute(
                text(
                    # The reason rides the flip's own statement: the terminal
                    # freeze makes the row immutable once `failed` lands.
                    "UPDATE post_intents SET state = 'failed',"
                    " last_error = CAST(:e AS jsonb)"
                    " WHERE id = :intent AND state = 'publishing' RETURNING id"
                ),
                {"intent": ctx.intent_id, "e": json.dumps(_error_of(exc))},
            )
        ).fetchone()
        if moved is None:
            raise ValueError(f"intent {ctx.intent_id} left 'publishing' during fail")
        await _say_outcome(
            session, ctx, state="failed", at=at, notice=_failure_notice(exc)
        )
        await finalize_job(session, ctx.job["id"], ctx.job["lease_token"], "failed")
    return FAILED


def _failure_notice(exc: BaseException) -> str:
    """The workspace's sentence for a terminal publish failure — the thing
    that failed and what to do, never the machine detail."""
    if isinstance(exc, DriveTerminalError):
        return (
            "A story didn't post: its file is missing from Drive or too large"
            " for Instagram. Open the Queue on the web to see which."
        )
    return "A story didn't post. Open the Queue on the web to see which."


async def _say_outcome(
    session,
    ctx: _Ctx,
    *,
    state: str,
    at,
    notice: str,
    reply_markup: Optional[dict] = None,
) -> int:
    """Restate every card of the intent with the outcome line (the posted
    line's own path — the tap already superseded the card, so only a restate
    by ref can reach it) and write ONE notification per push binding, in the
    caller's transaction. Returns how many bindings could hear it — zero
    means nobody was told, and the caller must not pretend otherwise."""
    return await _restate_and_notify(
        session,
        workspace_id=ctx.workspace_id,
        intent_id=ctx.intent_id,
        state=state,
        at=at,
        tz=ctx.tz,
        notice=notice,
        reply_markup=reply_markup,
    )


async def _restate_and_notify(
    session,
    *,
    workspace_id: str,
    intent_id: str,
    state: str,
    at,
    tz: str,
    notice: str,
    reply_markup: Optional[dict] = None,
) -> int:
    line = prompts.outcome_line(state, by=None, at=at, tz=tz)
    bindings = await prompts.push_bindings(session, workspace_id)
    for binding_id in bindings:
        await outbox.restate_cards(
            session,
            workspace_id=workspace_id,
            binding_id=binding_id,
            intent_id=intent_id,
            outcome_text=line,
            reply_markup=reply_markup,
        )
    await outbox.fanout_notification(
        session,
        workspace_id=workspace_id,
        bindings=bindings,
        text=notice,
        intent_id=intent_id,
    )
    return len(bindings)


# -- the float's safety nets (plan 03, UX principle 2) ----------------------
# An approval is never silently undone: the only exits from Approved are
# Posted and Needs review. A dead job and the reaper's safety net end on the
# review card with its three buttons and one honest line.

_INTENT_FOR_PARK = (
    "SELECT i.state, i.workspace_id, COALESCE(a.tz, w.tz) AS tz"
    "  FROM post_intents i"
    "  JOIN ig_accounts a ON a.id = i.ig_account_id"
    "  JOIN workspaces w ON w.id = i.workspace_id"
    " WHERE i.id = :intent"
)


async def park_for_review(
    session,
    *,
    workspace_id: str,
    intent_id: str,
    from_state: str,
    tz: str,
    notice: str,
    at: Optional[datetime] = None,
) -> bool:
    """`from_state → review_required` (edges 055 and 076), the card restated
    with the review keyboard, one notice per push binding, the
    `customer_notified` latch when anyone heard — in the caller's
    transaction. False when the row had already moved on."""
    at = at or datetime.now(timezone.utc)
    moved = (
        await session.execute(
            text(
                "UPDATE post_intents SET state = 'review_required'"
                " WHERE id = :intent AND state = :from_state RETURNING id"
            ),
            {"intent": intent_id, "from_state": from_state},
        )
    ).fetchone()
    if moved is None:
        return False
    told = await _restate_and_notify(
        session,
        workspace_id=workspace_id,
        intent_id=intent_id,
        state="review_required",
        at=at,
        tz=tz,
        notice=notice,
        reply_markup=prompts.review_keyboard(intent_id),
    )
    if told:
        await session.execute(
            text(
                "UPDATE post_intents SET "
                + intent_ledger.EVIDENCE_MERGE.format(
                    seed="{}", key="customer_notified", value="true"
                )
                + " WHERE id = :intent"
            ),
            {"intent": intent_id},
        )
    return True


async def park_exhausted(session, job: dict) -> bool:
    """A `publish_pipeline` job whose budget is spent (five untyped crashes):
    the story it carried is parked for review from wherever it stood —
    `publishing` mid-ladder or `approved` between attempts — with the line
    that says what is known. A story already settled parks nothing."""
    payload = job.get("payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    intent_id = payload.get("intent_id")
    if not intent_id:
        return False
    row = (
        (await session.execute(text(_INTENT_FOR_PARK), {"intent": str(intent_id)}))
        .mappings()
        .first()
    )
    if row is None or row["state"] not in ("publishing", "approved"):
        return False
    tries = (
        int(job.get("attempts") or job.get("max_attempts") or 0)
        or jobs.LANE_BUDGETS["bulk"][0]
    )
    return await park_for_review(
        session,
        workspace_id=str(row["workspace_id"]),
        intent_id=str(intent_id),
        from_state=row["state"],
        tz=str(row["tz"] or "UTC"),
        notice=(
            f"We hit a fault on our side {tries} times trying to post this story:"
            " choose on its card, or open the Queue on the web."
        ),
    )


async def park_stale_approved(session, *, older_than_seconds: int, limit: int) -> int:
    """The reaper's safety net (plan 03): an `approved` story the pipeline
    has not finished within the TTL is parked for review — never expired,
    which would undo a decision the workspace made without a word. A
    floating story re-enters `approved` at every hop, so its clock restarts
    and it never reaches the TTL; a paused workspace's stories wait on
    purpose and are not listed. Returns how many rows were moved.

    The reap job is a system singleton (`app.tenant_id = ''`), under which a
    plain SELECT here matches nothing once the worker runs as `svc_worker`
    (`p_tenant`; the reconciler's own lesson — both review lenses of #1306).
    So the stale rows are listed through `fn_reaper_stale_approved` (076,
    SECURITY DEFINER) and each row's tenant is asserted before it is
    touched, under a savepoint per row (one story's fault must not roll back
    the door's sweep), and the session's own scope is restored after — the
    settled-cards sweep reads across tenants in this same transaction next.
    A story flagged `cancel_requested` has no job left to honour the flag:
    the reaper does — the refund it carries first, then `cancelled` — rather
    than park a story the workspace gave up on."""
    rows = (
        await session.execute(
            text(
                "SELECT o_intent_id, o_workspace_id"
                "  FROM fn_reaper_stale_approved(make_interval(secs => :ttl), :lim)"
            ),
            {"ttl": int(older_than_seconds), "lim": int(limit)},
        )
    ).all()
    if not rows:
        return 0
    before = (
        await session.execute(
            text(
                "SELECT current_setting('app.tenant_id', true),"
                " current_setting('app.actor_kind', true)"
            )
        )
    ).one()
    before_tenant, before_actor = before[0] or "", before[1] or None
    days = max(1, round(int(older_than_seconds) / SECONDS_PER_DAY))
    moved = 0
    for intent_id, workspace_id in rows:
        try:
            async with session.begin_nested():
                moved += await _park_or_cancel_stale(
                    session,
                    intent_id=str(intent_id),
                    workspace_id=str(workspace_id),
                    days=days,
                )
        except Exception:  # noqa: BLE001 — isolated, logged, the sweep goes on
            logger.exception(
                "reaper: stale approved story %s skipped this tick", intent_id
            )
            continue
    await apply_gucs(session, tenant_id=before_tenant, actor_kind=before_actor)
    return moved


async def _park_or_cancel_stale(
    session, *, intent_id: str, workspace_id: str, days: int
) -> int:
    """One stale approved story, under its own tenant: cancelled with its
    refund when the workspace had asked for that, parked for review
    otherwise. Returns 1 when the row moved."""
    await apply_gucs(session, tenant_id=workspace_id, actor_kind="reaper")
    row = (
        (
            await session.execute(
                text(
                    "SELECT i.cancel_requested, i.cap_consumed_on, i.ig_account_id,"
                    "       COALESCE(a.tz, w.tz) AS tz"
                    "  FROM post_intents i"
                    "  JOIN ig_accounts a ON a.id = i.ig_account_id"
                    "  JOIN workspaces w ON w.id = i.workspace_id"
                    " WHERE i.id = :intent AND i.state = 'approved'"
                ),
                {"intent": intent_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return 0
    if row["cancel_requested"]:
        if row["cap_consumed_on"] is not None:
            await publish_cap.refund_cap(
                session,
                intent_id=intent_id,
                workspace_id=workspace_id,
                ig_account_id=str(row["ig_account_id"]),
            )
        cancelled = (
            await session.execute(
                text(
                    "UPDATE post_intents SET state = 'cancelled'"
                    " WHERE id = :intent AND state = 'approved' RETURNING id"
                ),
                {"intent": intent_id},
            )
        ).fetchone()
        return 1 if cancelled else 0
    parked = await park_for_review(
        session,
        workspace_id=workspace_id,
        intent_id=intent_id,
        from_state="approved",
        tz=str(row["tz"] or "UTC"),
        notice=(
            f"We lost track of this story for {days} days: choose on its card,"
            " or open the Queue on the web."
        ),
    )
    return 1 if parked else 0


async def _confirm(
    uow,
    ctx: _Ctx,
    *,
    permit: dict,
    media_id: str,
    transit,
    repost_ttl_days_default: int,
    now_fn=None,
) -> str:
    """The terminal domain transaction (`02` §4 publishing→posted row):
    permit outcome + posted flip (one statement, so `ck_posted_complete`
    holds at the row) + times_posted/recent-lock/last_posted_at + job
    finalization. After commit: best-effort inline FC-3.5 transit destroy —
    no job; the FC-3.6 sweep is the guarantee."""
    async with uow.begin() as session:
        await provider_ops.resolve_permit(
            session,
            op_id=permit["id"],
            outcome="succeeded",
            response_ref={"v": 1, "media_id": media_id},
        )
        moved = (
            await session.execute(
                text(
                    "UPDATE post_intents SET state = 'posted', last_error = NULL,"
                    " publish_step = 'effect_confirmed', ig_media_id = :mid"
                    " WHERE id = :intent AND state = 'publishing' RETURNING id"
                ),
                {"mid": media_id, "intent": ctx.intent_id},
            )
        ).fetchone()
        if moved is None:
            raise ValueError(f"intent {ctx.intent_id} left 'publishing' during confirm")
        # `04`'s effect list, in the ledger's one spelling (shared with the
        # manual path and the review card's "it posted").
        await intent_ledger.posted_effects(
            session,
            workspace_id=ctx.workspace_id,
            media_item_id=str(ctx.intent["media_item_id"]),
            ig_account_id=str(ctx.intent["ig_account_id"]),
            intent_id=ctx.intent_id,
            ttl_days=int(ctx.intent["repost_ttl_days"] or repost_ttl_days_default),
        )
        # The card said "✅ Approved by … — posting shortly"; now it says
        # posted. The tap already superseded the card (its buttons are gone),
        # so this is a RESTATE, not a supersede: every card of the intent that
        # still has a message to edit, in every push binding, gains the
        # terminal line — in THIS transaction, so a rolled-back confirm takes
        # the edit with it (the phase-1 follow-up `01_the-tap.md` named).
        line = prompts.outcome_line(
            "posted",
            by=None,
            at=(now_fn() if now_fn is not None else datetime.now(timezone.utc)),
            tz=ctx.tz,
        )
        await outbox.restate_everywhere(
            session,
            workspace_id=ctx.workspace_id,
            intent_id=ctx.intent_id,
            outcome_text=line,
        )
        await finalize_job(session, ctx.job["id"], ctx.job["lease_token"], "succeeded")
    try:
        await transit.destroy(
            ctx.intent["transit_asset_ref"], media_kind=ctx.intent["media_kind"]
        )
    except Exception:  # noqa: BLE001 — best-effort; FC-3.6 is the guarantee
        logger.warning(
            "inline FC-3.5 destroy failed for intent %s; the sweep will reap it",
            ctx.intent_id,
            exc_info=True,
        )
    return POSTED
