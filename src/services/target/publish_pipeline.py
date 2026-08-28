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

**Error 9 leaves the intent IN `publishing`.** There is no
`publishing → approved` edge in the seeded matrix, so "same path as a local
cap denial" (#915) means the defer MECHANICS — reschedule to the account's
next slot + `cap_deferred` audit row — not the intent state. The permit
resolves `failed` (error 9 is Meta saying the publish definitively did not
happen — exactly §6's "confirmed-safe failure"), so the next attempt permits
generation+1 and calls again. Key 4 stays held while parked, which is
CORRECT: a sibling intent on the same real account would hit the same cap.

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

No claim loop / process root (webhook_ingress's #903 note stands: no
composition root exists yet and this increment does not create one); no real
Meta adapter (stub/sandbox until M.3 — `meta_adapter`); no heartbeat wiring
(`LeaseHeartbeat` is the composition root's, per its own notes). Unexpected
exceptions PROPAGATE — a crashed worker must look crashed, so the lease
expires, the reaper re-readies, and resume-from-checkpoint does its job;
a blanket except here would convert crash-safety into silent corruption.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import text

from src.services.target import provider_ops, publish_cap
from src.services.target.jobs import assert_lease, finalize_job, reschedule_job
from src.services.target.meta_adapter import (
    MetaCapDeferral,
    MetaError,
    MetaLostResponse,
    MetaTerminalError,
)
from src.services.target.publish_cap import FlipOutcome, IntentNotApproved
from src.services.target.unit_of_work import apply_gucs, unit_of_work
from src.services.target.usage_precheck import DEFER

logger = logging.getLogger(__name__)

#: `05` row 8, bulk lane: backoff 1/5/15/60 min for retryable failures.
DEFAULT_BACKOFF_SECONDS = (60.0, 300.0, 900.0, 3600.0)

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
DEFERRED_CAP = "deferred_cap"
DEFERRED_META_CAP = "deferred_meta_cap"
PARKED_AMBIGUOUS = "parked_ambiguous"
RETRY_SCHEDULED = "retry_scheduled"
POISONED = "poisoned"
FAILED = "failed"
CANCELLED = "cancelled"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _Ctx:
    """One run's working set: the job, the intent row, account/workspace
    effective config, and permit state — read once, threaded explicitly."""

    def __init__(self, job: dict, intent: dict, ops: list[dict]):
        self.job = job
        self.intent = intent
        self.ops = ops

    @property
    def intent_id(self) -> str:
        return str(self.intent["id"])

    @property
    def workspace_id(self) -> str:
        return str(self.job["workspace_id"])

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
    now_fn: Callable[[], datetime] = _utcnow,
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
    if state in (
        "posted",
        "failed",
        "cancelled",
        "expired",
        "skipped",
        "rejected",
        "review_required",
    ):
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
        outcome = await _admit(uow, ctx, meta, precheck, backoff_seconds, now_fn)
        if outcome is not None:
            return outcome
        # Flip committed — the ladder proceeds as a publishing intent (the
        # SQL predicates enforce state DB-side; nothing re-reads the snapshot).

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
                        "       i.transit_asset_ref,"
                        "       i.provider_account_ref,"
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
                        "SELECT id, op_kind, state, generation, business_key"
                        "  FROM provider_operations WHERE intent_id = :intent"
                    ),
                    {"intent": str(intent_id)},
                )
            )
            .mappings()
            .all()
        )
    return _Ctx(job, dict(row), [dict(o) for o in ops])


def _local_date(ctx: _Ctx, now_fn) -> date:
    """The §4 debit day, in the account's effective tz — service-side, with
    the §0 fn_safe_tz rule applied at this site: an unresolvable zone
    degrades to UTC rather than aborting."""
    tz_name = ctx.intent["eff_tz"] or "UTC"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 — the §0 rule IS "degrade, never abort"
        tz = timezone.utc
    return now_fn().astimezone(tz).date()


def _next_slot(ctx: _Ctx, now_fn, backoff_seconds) -> datetime:
    """The deferral target: the account's next product slot, or one backoff
    rung when the clock has not stamped one yet."""
    slot = ctx.intent["next_slot_at"]
    if slot is not None and slot > now_fn():
        return slot
    return now_fn() + timedelta(seconds=backoff_seconds[0])


async def _audit_deferral(
    session, ctx: _Ctx, *, reason: str, next_run_at: datetime, state: str
) -> None:
    """The §4 'cap_deferred' audit row — the first direct (non-trigger) audit
    write in the codebase, so it reads the actor GUCs exactly the way the
    triggers do rather than trusting parameters."""
    await session.execute(
        text(
            "INSERT INTO audit_events (workspace_id, entity_kind, entity_id,"
            " from_state, to_state, actor_kind, actor_user_id, channel, detail)"
            " VALUES (:ws, 'post_intent', :intent, :state, :state,"
            "         current_setting('app.actor_kind'),"
            "         NULLIF(current_setting('app.actor_user_id', true), '')::uuid,"
            "         NULLIF(current_setting('app.channel', true), ''),"
            "         CAST(:detail AS jsonb))"
        ),
        {
            "ws": ctx.workspace_id,
            "intent": ctx.intent_id,
            "state": state,
            "detail": json.dumps(
                {
                    "v": 1,
                    "event": "cap_deferred",
                    "reason": reason,
                    "next_run_at": next_run_at.isoformat(),
                }
            ),
        },
    )


async def _admit(
    uow, ctx: _Ctx, meta, precheck, backoff_seconds, now_fn
) -> Optional[str]:
    """The admission gate for an `approved` intent: cancel honor, the §8
    advisory pre-check, then the §4 flip. Returns an outcome to stop with,
    or None to continue into the ladder."""
    if ctx.intent["cancel_requested"]:
        async with _leased_tx(uow, ctx.job) as session:
            cancelled = (
                await session.execute(
                    text(
                        "UPDATE post_intents SET state = 'cancelled'"
                        " WHERE id = :intent AND state = 'approved' RETURNING id"
                    ),
                    {"intent": ctx.intent_id},
                )
            ).fetchone()
            await finalize_job(
                session, ctx.job["id"], ctx.job["lease_token"], "cancelled"
            )
        if cancelled is None:
            raise ValueError(
                f"intent {ctx.intent_id} left 'approved' during cancel honor"
            )
        return CANCELLED

    if precheck is not None:
        # A provider read — deliberately OUTSIDE any transaction (`02` §8:
        # "immediately before the §4 flip transaction").
        verdict = await precheck.check(meta, ctx.intent["provider_account_ref"])
        if verdict == DEFER:
            run_at = _next_slot(ctx, now_fn, backoff_seconds)
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
            return DEFERRED_META_CAP

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
            if flip is FlipOutcome.DEFERRED:
                run_at = _next_slot(ctx, now_fn, backoff_seconds)
                await reschedule_job(
                    session,
                    ctx.job["id"],
                    ctx.job["lease_token"],
                    run_at=run_at,
                    restore_attempt=True,
                )
                await _audit_deferral(
                    session, ctx, reason="cap", next_run_at=run_at, state="approved"
                )
                deferred = True
            else:
                deferred = False
    except IntentNotApproved:
        # (1,0): a race moved the intent while we held the job. The UoW
        # rolled back (debit included). Route on what it became.
        async with uow.begin() as session:
            current = (
                await session.execute(
                    text("SELECT state FROM post_intents WHERE id = :intent"),
                    {"intent": ctx.intent_id},
                )
            ).scalar_one()
            if current in (
                "posted",
                "failed",
                "cancelled",
                "expired",
                "skipped",
                "rejected",
            ):
                await finalize_job(
                    session, ctx.job["id"], ctx.job["lease_token"], "cancelled"
                )
            else:
                raise
        return CANCELLED
    return DEFERRED_CAP if deferred else None


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


async def _retry_or_poison(
    uow,
    ctx: _Ctx,
    backoff_seconds,
    now_fn,
    *,
    resolve_op_id=None,
    resolve_response: Optional[dict] = None,
    step_back_to: Optional[str] = None,
) -> str:
    """R8's retryable-failure edge: reschedule on the `05` ladder while the
    attempts budget holds; G5 poison (`publishing → review_required`, debit
    retained) when it is exhausted. *resolve_op_id* records a definitive
    failure on the permit in the same transaction; *step_back_to* rewinds the
    ladder (the dead-container case)."""
    attempts = int(ctx.job["attempts"])
    exhausted = attempts >= int(ctx.job["max_attempts"])
    async with _leased_tx(uow, ctx.job) as session:
        if resolve_op_id is not None:
            await provider_ops.resolve_permit(
                session,
                op_id=resolve_op_id,
                outcome="failed",
                response_ref=resolve_response,
            )
        # `step_back_to` is the caller saying the artifacts BEYOND that rung are
        # abandoned — today only the dead-container paths pass it. So it is also
        # the signal for whether `ig_container_id` still points at anything, and
        # it is the right discriminator on BOTH branches below.
        #
        # It must NOT be a blanket clear on poison. Poison is shared by every
        # retryable class: a poison after a failed PUBLISH leaves a container
        # that is still live and reusable, and nulling its id there would throw
        # away a valid pointer. Only a caller that rewound past the container
        # rung is telling us the container is gone (#938).
        drop_container = ", ig_container_id = NULL" if step_back_to else ""
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
        rung = backoff_seconds[min(attempts - 1, len(backoff_seconds) - 1)]
        await reschedule_job(
            session,
            ctx.job["id"],
            ctx.job["lease_token"],
            run_at=now_fn() + timedelta(seconds=rung),
            restore_attempt=False,
        )
    return RETRY_SCHEDULED


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
        media = await media_fetch(dict(ctx.intent))
        ref = await transit.upload(
            media,
            workspace_id=ctx.workspace_id,
            media_kind=ctx.intent["media_kind"],
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
        generation = ctx.next_generation("container_create")
        permit = await _permit(
            engine, ctx, op_kind="container_create", generation=generation
        )
        media_url = transit.delivery_url(
            ctx.intent["transit_asset_ref"], media_kind=ctx.intent["media_kind"]
        )
        try:
            container_id = await meta.create_container(
                ctx.intent["provider_account_ref"],
                media_url=media_url,
                media_kind=ctx.intent["media_kind"],
            )
        except MetaTerminalError as exc:
            return await _fail_terminal(uow, ctx, op_id=permit["id"], exc=exc)
        except MetaError as exc:
            return await _retry_or_poison(
                uow,
                ctx,
                backoff_seconds,
                now_fn,
                resolve_op_id=permit["id"],
                resolve_response={"v": 1, "error": exc.code},
            )
        except MetaLostResponse:
            # Lost response on a RECOVERABLE effect (`02` §6): resolve it the
            # way a crash-resume would — failed/lost_response — and retry on
            # the ladder. Never intent-level ambiguity for a container.
            # (Anything untyped propagates: a crash must look like a crash.)
            return await _retry_or_poison(
                uow,
                ctx,
                backoff_seconds,
                now_fn,
                resolve_op_id=permit["id"],
                resolve_response={"v": 1, "error": "lost_response"},
            )
        async with _leased_tx(uow, ctx.job) as session:
            await provider_ops.resolve_permit(
                session,
                op_id=permit["id"],
                outcome="succeeded",
                response_ref={"v": 1, "container_id": container_id},
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
        ctx.intent["ig_container_id"] = container_id
        step = "container_created"

    if step == "container_created":
        verdict = await _await_ready(ctx, meta, sleep)
        if verdict == "dead":
            # The container is definitively gone; a NEW one needs a fresh
            # create (and a fresh generation) from the still-valid transit
            # asset — step back, retry on the ladder.
            return await _retry_or_poison(
                uow, ctx, backoff_seconds, now_fn, step_back_to="transit_uploaded"
            )
        if verdict == "pending":
            return await _retry_or_poison(uow, ctx, backoff_seconds, now_fn)
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
        verdict = await _await_ready(ctx, meta, sleep)
        if verdict == "dead":
            return await _retry_or_poison(
                uow, ctx, backoff_seconds, now_fn, step_back_to="transit_uploaded"
            )
        if verdict == "pending":
            return await _retry_or_poison(uow, ctx, backoff_seconds, now_fn)
        step = "container_ready"

    if step == "container_ready":
        generation = ctx.next_generation("publish")
        permit = await _permit(engine, ctx, op_kind="publish", generation=generation)
        try:
            media_id = await meta.publish(
                ctx.intent["provider_account_ref"], ctx.intent["ig_container_id"]
            )
        except MetaCapDeferral as exc:
            # Error 9 (`02` §8): a cap, not a fault. Definitive non-effect →
            # resolve failed; defer to the next slot; debit and key 4 stand.
            run_at = _next_slot(ctx, now_fn, backoff_seconds)
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
                await _audit_deferral(
                    session,
                    ctx,
                    reason="error_9",
                    next_run_at=run_at,
                    state="publishing",
                )
            return DEFERRED_META_CAP
        except MetaTerminalError as exc:
            return await _fail_terminal(uow, ctx, op_id=permit["id"], exc=exc)
        except MetaError as exc:
            return await _retry_or_poison(
                uow,
                ctx,
                backoff_seconds,
                now_fn,
                resolve_op_id=permit["id"],
                resolve_response={"v": 1, "error": exc.code},
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
        )

    if step == "effect_confirmed":
        raise ValueError(
            f"intent {ctx.intent_id}: step effect_confirmed with state "
            "publishing — the terminal tx writes both together"
        )
    raise ValueError(f"intent {ctx.intent_id}: unknown publish_step {step!r}")


async def _await_ready(ctx: _Ctx, meta, sleep) -> str:
    """One bounded readiness segment: 'ready' | 'dead' | 'pending'."""
    for attempt in range(POLL_BUDGET):
        status = await meta.container_status(ctx.intent["ig_container_id"])
        if status in _READY_STATUSES:
            return "ready"
        if status in _DEAD_STATUSES:
            return "dead"
        if attempt + 1 < POLL_BUDGET:
            await sleep(POLL_INTERVAL_S)
    return "pending"


async def _fail_terminal(uow, ctx: _Ctx, *, op_id, exc: MetaError) -> str:
    """`publishing → failed` on a definitive permanent provider error: permit
    failed + state flip + cap refund + job failed, ONE transaction (`02` §4:
    the refund rides the terminal flip's transaction)."""
    async with _leased_tx(uow, ctx.job) as session:
        await provider_ops.resolve_permit(
            session,
            op_id=op_id,
            outcome="failed",
            response_ref={"v": 1, "error": exc.code, "terminal": True},
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
                    "UPDATE post_intents SET state = 'failed'"
                    " WHERE id = :intent AND state = 'publishing' RETURNING id"
                ),
                {"intent": ctx.intent_id},
            )
        ).fetchone()
        if moved is None:
            raise ValueError(f"intent {ctx.intent_id} left 'publishing' during fail")
        await finalize_job(session, ctx.job["id"], ctx.job["lease_token"], "failed")
    return FAILED


async def _confirm(
    uow,
    ctx: _Ctx,
    *,
    permit: dict,
    media_id: str,
    transit,
    repost_ttl_days_default: int,
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
                    "UPDATE post_intents SET state = 'posted',"
                    " publish_step = 'effect_confirmed', ig_media_id = :mid"
                    " WHERE id = :intent AND state = 'publishing' RETURNING id"
                ),
                {"mid": media_id, "intent": ctx.intent_id},
            )
        ).fetchone()
        if moved is None:
            raise ValueError(f"intent {ctx.intent_id} left 'publishing' during confirm")
        await session.execute(
            text(
                "UPDATE media_items SET times_posted = times_posted + 1,"
                " last_posted_at = now() WHERE id = :media"
            ),
            {"media": str(ctx.intent["media_item_id"])},
        )
        await session.execute(
            text(
                "INSERT INTO post_locks (workspace_id, media_item_id, kind,"
                " ig_account_id, expires_at)"
                " VALUES (:ws, :media, 'recent', :acct,"
                "         now() + make_interval(days => :ttl_days))"
                " ON CONFLICT (workspace_id, media_item_id, kind, ig_account_id)"
                "   WHERE ig_account_id IS NOT NULL"
                " DO UPDATE SET expires_at = EXCLUDED.expires_at"
            ),
            {
                "ws": ctx.workspace_id,
                "media": str(ctx.intent["media_item_id"]),
                "acct": str(ctx.intent["ig_account_id"]),
                "ttl_days": int(
                    ctx.intent["repost_ttl_days"] or repost_ttl_days_default
                ),
            },
        )
        await session.execute(
            text("UPDATE ig_accounts SET last_posted_at = now() WHERE id = :acct"),
            {"acct": str(ctx.intent["ig_account_id"])},
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
