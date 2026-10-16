"""The `offboard_workspace` job — the five offboarding legs, orchestrated.

The executor orchestrates the legs — drain → revoke → transit reap → grace →
`fn_offboard_finalize` — and the command only enqueues it. The door (`04_functions.sql`)
and the job kind (`machinery.py`) are what it calls, in order.

## Leg order is load-bearing, and it is not the obvious one

Revoking credentials **before** draining
publishing work would destroy exactly the credentials reconciliation still
needs to find out whether a post went out. So: drain first, with credentials
alive; revoke second; transit reap third; then the grace window; then deletion.
A drain that does not finish inside its timeout **parks rather than
proceeding** — revoking under live work is the failure that ordering exists to
prevent, and "we waited long enough" is not a reason to cause it.

## Every leg is idempotent, because the job runs more than once

Two runs is the normal shape: one drains, revokes, reaps and schedules the
finalizer for the end of the grace window; the second finalizes. A lease that
expires mid-run adds a third that finds legs 1–3 already done. So each leg is
written as a statement that is a no-op the second time — `state <> 'revoked'`,
`transit_asset_ref IS NOT NULL`, a cancel bounded to non-terminal states —
rather than as a step guarded by a progress marker. There is no progress
marker: the rows are the progress.

## Two interactions with the work loop, and how the job handles each

**The finalize leg deletes its own job row.** `jobs.workspace_id` is
``REFERENCES workspaces(id) ON DELETE CASCADE`` (`machinery.py`), so the moment
`fn_offboard_finalize` deletes the workspace the job disappears with it. If that
DELETE rode the loop's session, the loop's own `finalize_job` would then match
zero rows, raise `JobFenced` **inside** the session context, and the context
manager would roll the deletion back — an offboard that can never complete and
retries forever. So the finalize leg owns its own transaction (the
`publish_pipeline` / `revoke_workspace_credentials` precedent), commits, and the
fence the loop logs afterwards is expected: it is the job observing its own
erasure, not another owner winning. It is announced on the line above it for the
same reason `send_email` announces its deferral — that warning is what an
operator reads to answer "is the reaper racing my workers".

**The successor is minted, not self-rescheduled.** A handler that rescheduled
itself would hit the same rollback, so a run that is not the last one mints the
NEXT `offboard_workspace` job in the loop's session and returns normally; the
loop finalizes the current job `succeeded` and both commit together.
`jobs.enqueue`'s `unless_pending` cannot be used for that — the current job is
itself `leased` on the same serialization key and would decline the mint — so
the duplicate guard is explicit and excludes the current row.

## The archive's transit drain runs only in a run that has written nothing

An archive upload that was admitted but not yet settled or destroyed blocks
`fn_offboard_finalize` (its guard names it), so leg 3 also drains those:
`archive_transit_cleanup.settle_and_destroy`, after the archives are fenced and
before the credentials are revoked. That function takes the pipeline locks of
the intents it settles, in transactions of its own, and asks the provider
between them. So it must run while the loop's session holds no row lock the
drain could need, and a session that has cancelled an intent holds exactly the
one it waits on: the run would wait on itself, and finalize behind it. The drain
therefore runs only in a run whose first leg cancelled nothing, which is every
run but the first of a workspace that had live work, and the cancelling run
defers it. It does not lose it: that run always schedules a successor, and the
successor finds the intents terminal and the credentials revoked, so it writes
nothing before the drain. Revoking waits for the drain in the same run for the
same reason.

A drain that reports an upload still owed holds the finalize: the run writes an
`offboard_archive_transit_unresolved` audit row with the counts, mints a
successor after the recheck interval and returns `archive_transit_pending`. An
upload that is overdue (unsettled past the drain's review horizon) is asked
about once an hour instead, so one that never resolves writes one audit row an
hour rather than one a minute. A finalize the guard would refuse is a raise that
spends the retry budget and strands the workspace, so only a run whose drain
reports nothing owed finalizes (each drain takes a bounded batch, so a large
backlog is several turns), and the guard in `fn_offboard_finalize` stays as the
backstop where no drain ran.

## What is inert in a manual-mode workspace

Legs 1 and 3 have nothing to act on in a manual-mode workspace, and that is a
property of the data rather than of this code. `publishing` and
`publishing_ambiguous` are reachable only through the publish pipeline, and
none of its producers (`jobs.mint_publish_job`) runs without
`api_publishing_enabled`: `approve` and `resolve_review` refuse with
`manual_mode`, the automatic approval declines (`api_off`), and a give-up mints
one only for a story that reached the transit upload. The same gate means no
intent carries a `transit_asset_ref`. Both legs are built, and act on such rows
wherever they exist.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import text

from src.exceptions.base import StorydumpError
from src.services.target import (
    archive_lifecycle,
    archive_transit_cleanup,
    audit,
    intent_ledger,
    jobs,
    unit_of_work,
)
from src.services.target.intent_ledger import IntentTransitionRefused

logger = logging.getLogger(__name__)

#: The registry's lane and serialization key for the kind.
LANE = "bulk"

#: The terminal intent states, from the one Python home. This module and
#: `fn_offboard_finalize` MUST agree on this set: leg 1 cancels `NOT terminal`
#: and the door refuses while any `NOT terminal` row survives, so a private copy
#: that drifted would mint successors forever.
TERMINAL_STATES = intent_ledger.TERMINAL_STATES

#: A cancel is legal from every WORKING state except these two, which must
#: DRAIN rather than be flipped.
DRAINING_STATES = ("publishing", "publishing_ambiguous")

#: The most cancels one run makes. Each rides its own savepoint, and a
#: savepoint that writes takes a subtransaction id; a backend caches 64 of them
#: (`PGPROC_MAX_CACHED_SUBXIDS`). Past that, every other backend's snapshot is
#: marked suboverflowed until this transaction ends, and their visibility
#: checks fall back to `pg_subtrans`. The drain's are the only
#: savepoints its transaction takes. A refused cancel's savepoint rolls back
#: and its id leaves the cache, so only a cancel counts.
MAX_WRITING_SAVEPOINTS = 64

#: How long an offboarding waits to ask again while an archive upload it is held
#: on is overdue (`archive_transit_cleanup.DRAIN_REVIEW_AFTER_SECONDS`). It can
#: still land, so it is asked about again, but it is not about to: every turn
#: costs a provider question and an audit row for the same unresolved fact, so
#: the cadence is an hour, and never below the configured recheck.
OVERDUE_TRANSIT_RECHECK_SECONDS = 3600

#: The `run_at` of a successor due after a recheck interval (bind `:recheck`, in
#: seconds), at the two places a run waits on something it cannot finish.
_AFTER_RECHECK = "now() + (interval '1 second' * CAST(:recheck AS bigint))"

#: The grace window, and the ONE definition of it. `WorkerConfig` defaults
#: to this and `restore_workspace` refuses past it, so the finalizer and the way
#: back cannot disagree about when the window closed — the same reason
#: `finalize` takes the grace it was scheduled with rather than reading its own.
#:
#: The clock guard is POLICY, not safety. Safety is structural: `fn_offboard_finalize`
#: deletes the workspace row, so a restore attempted after it ran finds nothing
#: and refuses `not_found` on its own. This constant is what makes "irreversible
#: after the grace window" true BEFORE the finalizer happens to run.
GRACE_SECONDS_DEFAULT = 30 * 24 * 3600


def restorable_until(offboarding_at: Optional[datetime]) -> Optional[datetime]:
    """When the grace window closes for a workspace that began offboarding at
    *offboarding_at*, or None for one that is not offboarding.

    The dashboard shows this date and `restore_workspace` enforces the same
    arithmetic in SQL; both read `GRACE_SECONDS_DEFAULT`, so the screen never
    carries a copied "30 days" that could drift from the guard.
    """
    if offboarding_at is None:
        return None
    return offboarding_at + timedelta(seconds=GRACE_SECONDS_DEFAULT)


def serialization_key(workspace_id: str) -> str:
    return f"ws:{workspace_id}"


class DrainTimedOut(StorydumpError):
    """Leg 1's park: live publishing work outlived the drain
    timeout, so the workflow stops here rather than revoking under it."""


async def _live_publishing(session, workspace_id: str) -> int:
    row = (
        await session.execute(
            text(
                "SELECT count(*) FROM post_intents"
                " WHERE workspace_id = :ws AND state = ANY(:states)"
            ),
            {"ws": workspace_id, "states": list(DRAINING_STATES)},
        )
    ).first()
    return int(row[0])


async def drain(session, workspace_id: str, *, limit: int) -> dict[str, Any]:
    """Leg 1 — terminalize every live intent, with credentials still alive.

    The cancel goes through `intent_ledger.transition` rather than a bulk
    UPDATE so `trg_intent_guard` rules on each edge; cancel is
    legal from every working state except publishing/ambiguous, and the guard
    is where that is actually enforced. A refusal on one intent is returned
    for the caller to record, and the rest continue — one unexpected edge must
    not strand the whole offboard.

    It reads up to *limit* live intents and stops after
    :data:`MAX_WRITING_SAVEPOINTS` cancels; ``more`` says intents may be left.
    """
    rows = (
        await session.execute(
            text(
                "SELECT id::text FROM post_intents"
                " WHERE workspace_id = :ws"
                "   AND NOT (state = ANY(:terminal))"
                "   AND NOT (state = ANY(:draining))"
                " ORDER BY id"
                " LIMIT :lim"
            ),
            {
                "ws": workspace_id,
                "terminal": list(TERMINAL_STATES),
                "draining": list(DRAINING_STATES),
                "lim": limit,
            },
        )
    ).all()
    cancelled, processed, refusals = 0, 0, []
    for (intent_id,) in rows:
        if cancelled == MAX_WRITING_SAVEPOINTS:
            break
        processed += 1
        # A refusal aborts the transaction; the savepoint is what lets the drain
        # go on to the next intent after one (`intent_ledger.transition`).
        try:
            async with session.begin_nested():
                await intent_ledger.transition(session, intent_id, "cancelled")
            cancelled += 1
        except IntentTransitionRefused as exc:
            logger.warning(
                "offboard drain: intent %s refused the cancel edge (%s); leaving"
                " it for the finalize guard to catch",
                intent_id,
                exc,
            )
            refusals.append({"intent_id": intent_id, "refusal": str(exc)})
    return {
        "cancelled": cancelled,
        "refused": len(refusals),
        "more": processed < len(rows) or len(rows) == limit,
        "refusals": refusals,
    }


async def fence_archives(factory, workspace_id: str) -> int:
    """Leg 2's archive half, before the credentials go: every
    archive is fenced through the epoch writer (`archive_lifecycle.offboard_fence`),
    so no new admission or permit outlives the revoke. Returns how many.

    **In its OWN transaction, committed before leg 2.** The fence takes the
    workspace's source prefix, and the canonical order puts that before any
    intent lock, but the loop's session already holds the drain's. Inside that
    session the prefix would also hold the workspace row and its sources until
    the run ends, and a finalize in the same run would wait on them forever.
    Committed here, it holds neither. Its effects are durable whatever the
    run does next, and a second run finds every archive fenced already.
    """
    async with factory() as session:
        fenced = await archive_lifecycle.offboard_fence(
            session, workspace_id=workspace_id
        )
        await session.commit()
    return len(fenced)


async def revoke_credentials(session, workspace_id: str) -> int:
    """Leg 2 — every credential goes `revoked` here, and the best-effort
    provider call rides its own job (`revoke_workspace_credentials`). A
    revocation that still fails is recorded and abandoned, because the row
    dies with the workspace and the provider token expires on its own.
    """
    revoked = (
        await session.execute(
            text(
                "UPDATE oauth_credentials SET state = 'revoked'"
                " WHERE workspace_id = :ws AND state <> 'revoked'"
                " RETURNING id::text"
            ),
            {"ws": workspace_id},
        )
    ).all()
    for (credential_id,) in revoked:
        await jobs.enqueue(
            session,
            kind="revoke_workspace_credentials",
            workspace_id=workspace_id,
            lane=LANE,
            serialization_key=f"revoke:{credential_id}",
            payload={"v": 1, "credential_id": credential_id},
            unless_pending=True,
        )
    return len(revoked)


async def reap_transit(session, workspace_id: str, *, transit) -> dict[str, Any]:
    """Leg 3 — destroy this workspace's live transit assets.

    *transit* is `deps.transit`, or None when no transit store is configured.
    With none, the refs are LEFT and reported: the TTL
    sweep is the backstop and the assets are TTL-bounded in minutes regardless,
    so a missing seam delays nothing the offboard is responsible for. Reporting
    rather than silently passing is the difference between a documented
    backstop and an unnoticed skip.

    **`destroy(ref, media_kind=…)`, not `destroy_asset(row)`** — the two doors
    are not interchangeable and picking the wrong one is silent. `destroy_asset`
    takes a `list_stale` ROW (`{"public_id", "resource_type"}`) and is the
    TTL sweep's deleter; `destroy` takes a bare ref plus the media kind and
    is the per-asset door, the one leg 3 uses.

    `media_kind` is joined from `media_items` because `_resource_type` needs it
    and `ck_media_kind` is closed; a ref without it cannot be addressed.

    **The ref column is not cleared, and cannot be.** Leg 1 has already
    terminalized every intent and `trg_intent_terminal_freeze` makes a terminal
    row immutable, so a successful destroy leaves the ref in place.
    Nor does it need clearing: the row dies at leg 5 either way. The
    cost is that a re-run re-attempts a destroy the provider has already done,
    which is one no-op call per asset on the normal two-run path — and is why
    this leg is deliberately excluded from the caller's "did this run change
    anything" test, since counting it would schedule a successor forever.
    """
    rows = (
        await session.execute(
            text(
                "SELECT i.transit_asset_ref, m.media_kind"
                "  FROM post_intents i"
                "  JOIN media_items m ON m.id = i.media_item_id"
                " WHERE i.workspace_id = :ws AND i.transit_asset_ref IS NOT NULL"
            ),
            {"ws": workspace_id},
        )
    ).all()
    if transit is None:
        return {"reaped": 0, "left_to_ttl": len(rows), "seam": "absent"}
    reaped, failed = 0, 0
    for ref, media_kind in rows:
        try:
            # A False answer is a refusal, not an error: `destroy` returns True
            # iff the asset is gone NOW, and counts "not found" as gone.
            gone = await transit.destroy(ref, media_kind=media_kind)
        except Exception:  # noqa: BLE001 — best-effort; the TTL sweep backstops
            logger.warning("offboard transit reap: %s raised; left to TTL", ref)
            failed += 1
            continue
        if gone:
            reaped += 1
        else:
            logger.warning("offboard transit reap: %s refused; left to TTL", ref)
            failed += 1
    return {"reaped": reaped, "left_to_ttl": failed, "seam": "wired"}


async def drain_archive_transit(deps, workspace_id: str) -> dict[str, Any]:
    """Leg 3's archive half: settle and destroy every archive upload the
    workspace admitted, so the finalize guard has nothing left to refuse on.

    **Call it only while the loop's session holds no write** (the module
    docstring says why). It asks the provider and never re-uploads; an upload
    the provider cannot account for stays an obligation, counted in
    `unresolved`, and the run does not finalize while one remains (it waits
    for a successor). `fn_offboard_finalize` refuses over it as well, and is
    the backstop.

    With no transit store nothing is asked or destroyed, but what the rows still
    owe is counted all the same: an attempt admitted while a store was configured
    outlives it, and the run waits on it (a successor) instead of finalizing
    into the guard's refusal.
    """
    drained = await archive_transit_cleanup.settle_and_destroy(
        deps.engine,
        workspace_id=workspace_id,
        transit=deps.transit,
        limit=archive_transit_cleanup.DRAIN_LIMIT,
    )
    return {
        "seam": "absent" if deps.transit is None else "wired",
        "settled": drained.settled,
        "destroyed": drained.destroyed,
        "unresolved": drained.unresolved,
        "overdue": drained.overdue,
    }


async def _audit(factory, workspace_id: str, event: str, *details: dict) -> None:
    """A parked leg writes no row of its own — `workspaces` carries
    `trg_governance_audit` only for UPDATEs to itself, and a drain that stalls
    updates nothing — so it writes one here, as it does for each refused
    cancel, whose savepoint rolled its own trace back. One row per *detail*.

    **In its OWN transaction, which is the whole point.** The park raises
    immediately afterwards, and a record written in the raising transaction is
    rolled back with it: the durable signal for a parked drain would be exactly
    as durable as no signal at all. The refusals are written before the park
    raises, one row per refused attempt: a run that reads the intent again
    records it again. Same reason
    `credential_lifecycle._audit_revoke_failed` opens its own.
    """
    async with factory() as session:
        for detail in details:
            await audit.record(
                session,
                workspace_id=workspace_id,
                entity_kind="workspace",
                entity_id_sql="CAST(:ws AS uuid)",
                from_state="offboarding",
                to_state="offboarding",
                detail={"v": 1, "event": event, **detail},
                actor_sql=audit.ACTOR_SYSTEM_CHANNEL,
            )
        await session.commit()


async def _mint_successor(
    session, job, workspace_id: str, run_at_sql: str, params: dict
) -> Optional[str]:
    """The next run of this workflow. `jobs.enqueue` cannot express a future
    `run_at` (it hardcodes `now()`, which is right for every other producer),
    and `unless_pending` cannot be used at all here because THIS job holds the
    same serialization key, so the insert and its duplicate guard are written
    out. The guard excludes the current row by id — without that exclusion it
    would never mint, and with no guard at all a re-claimed lease would mint a
    second workflow for one workspace.

    The budget comes from the lane rather than being written out, and the
    deadline is measured from `run_at`, **not** from `now()` as `enqueue`
    measures it. `enqueue`'s two are the same instant because it mints at
    `now()`; here they are up to `GRACE_SECONDS_DEFAULT` apart, so a copied
    `now() + 6 h` would be thirty days stale before the finalizer was ever
    runnable and `budget_exhausted` would end the workflow on its first
    claim. The lane's six hours are six hours of retrying, from the moment
    the job is due."""
    key = serialization_key(workspace_id)
    attempts, deadline_seconds = jobs.LANE_BUDGETS[LANE]
    row = (
        await session.execute(
            text(
                "INSERT INTO jobs (kind, workspace_id, lane, serialization_key,"
                " run_at, max_attempts, deadline_at, payload)"
                " SELECT 'offboard_workspace', CAST(:ws AS uuid), CAST(:lane AS text),"
                "        CAST(:key AS text), due.at, CAST(:attempts AS int),"
                "        due.at + make_interval(secs => CAST(:deadline AS int)),"
                "        CAST(:p AS jsonb)"
                f"   FROM (SELECT {run_at_sql} AS at) due"
                " WHERE NOT EXISTS (SELECT 1 FROM jobs"
                "                    WHERE serialization_key = :key"
                "                      AND kind = 'offboard_workspace'"
                "                      AND state IN ('ready', 'leased')"
                "                      AND id <> CAST(:self AS uuid))"
                " RETURNING id::text"
            ),
            {
                "ws": workspace_id,
                "lane": LANE,
                "key": key,
                "attempts": attempts,
                "deadline": deadline_seconds,
                "p": json.dumps({"v": 1}),
                "self": str(job["id"]),
                **params,
            },
        )
    ).first()
    return None if row is None else row[0]


async def finalize(factory, workspace_id: str, grace_seconds: int) -> None:
    """Leg 5, in its OWN transaction — see the module docstring. The door does
    the guarding; this only calls it with the same grace the scheduling used, so
    the two cannot disagree about when the window closed."""
    async with factory() as session:
        await session.execute(
            text(
                "SELECT fn_offboard_finalize(CAST(:ws AS uuid),"
                "       interval '1 second' * CAST(:g AS bigint))"
            ),
            {"ws": workspace_id, "g": grace_seconds},
        )
        # `poller_session_factory` yields an uncommitted session — closing it
        # rolls back. Without this line the workspace is deleted and then
        # un-deleted, and the workflow retries forever having reported success.
        await session.commit()


async def execute_offboard(deps, session, job) -> dict[str, Any]:
    """The five offboarding legs. Returns what it did, for the loop's log.

    *session* is the loop's transaction: legs 1–3 and the successor mint ride
    it. Leg 5 does not, and the module docstring says why.
    """
    cfg = deps.config
    workspace_id = str(job["workspace_id"])
    # One read, both deadlines. `now()` is `transaction_timestamp()` and does
    # not advance inside a transaction, so separate reads would
    # agree; one read is simply the clearer statement.
    row = (
        await session.execute(
            text(
                "SELECT state,"
                "  offboarding_at + (interval '1 second' * CAST(:d AS bigint))"
                "    <= now() AS drain_expired,"
                "  offboarding_at + (interval '1 second' * CAST(:g AS bigint))"
                "    <= now() AS grace_elapsed"
                " FROM workspaces WHERE id = :ws"
            ),
            {
                "ws": workspace_id,
                "d": cfg.offboard_drain_timeout_seconds,
                "g": cfg.offboard_grace_seconds,
            },
        )
    ).first()
    if row is None:
        # Already finalized — a duplicate or a re-claimed lease arriving after
        # the cascade. Nothing to do and nothing wrong.
        return {"outcome": "already_finalized"}
    if row[0] != "offboarding":
        # Restored inside the grace window, or never offboarding.
        # The workflow stops; it does not un-cancel anything, which is exactly
        # what restore means (state + mandatory reconnect).
        return {"outcome": "not_offboarding", "state": row[0]}

    own_tx = unit_of_work.poller_session_factory(deps.engine, workspace_id)
    drained = await drain(session, workspace_id, limit=cfg.offboard_drain_limit)
    refusals = drained.pop("refusals")
    if refusals:
        await _audit(own_tx, workspace_id, "offboard_cancel_refused", *refusals)
    # A run the write bound stopped drains again now, not at the recheck or the
    # end of the grace window. A run that stopped for any other reason
    # does not: a read window of refusals would otherwise turn into back-to-back
    # runs of a few cancels each, recording its refusals every time.
    again = drained["more"] and drained["cancelled"] == MAX_WRITING_SAVEPOINTS
    still_publishing = await _live_publishing(session, workspace_id)
    if still_publishing:
        if row[1]:  # drain_expired
            # Park, do not revoke under live work. There is no operator alert
            # channel in this tier, so the loudest durable signals available
            # are used: an audit row, and a raise that spends the job's retry
            # budget and lands as a dead job row.
            await _audit(
                own_tx,
                workspace_id,
                "offboard_drain_timeout",
                {"publishing": still_publishing},
            )
            raise DrainTimedOut(
                f"workspace {workspace_id}: {still_publishing} intent(s) still"
                f" publishing past the {cfg.offboard_drain_timeout_seconds}s"
                " drain timeout; not revoking under live work"
            )
        if again:
            run_at_sql, params = "now()", {}
        else:
            run_at_sql = _AFTER_RECHECK
            params = {"recheck": cfg.offboard_drain_recheck_seconds}
        successor = await _mint_successor(
            session, job, workspace_id, run_at_sql, params
        )
        return {
            "outcome": "draining",
            "publishing": still_publishing,
            "successor": successor,
            **drained,
        }
    if again:
        # Legs 2-3 wait for the drain to finish: revoke stays after the last
        # cancel, and the transit reap runs once rather than once a batch.
        successor = await _mint_successor(session, job, workspace_id, "now()", {})
        return {"outcome": "draining", "successor": successor, **drained}

    archives = await fence_archives(own_tx, workspace_id)
    # Before the first write this session makes in this run. A run that
    # cancelled intents holds their locks, which the drain's own transactions
    # would wait on: it defers, and its successor drains (module docstring).
    archive_transit = (
        {"seam": "deferred"}
        if drained["cancelled"]
        else await drain_archive_transit(deps, workspace_id)
    )
    revoked = await revoke_credentials(session, workspace_id)
    transit = await reap_transit(session, workspace_id, transit=deps.transit)
    report = {
        "revoked": revoked,
        "archives_fenced": archives,
        "archive_transit": archive_transit,
        "transit": transit,
        **drained,
    }

    # **The finalizer is always a LATER run, even when the window has already
    # closed.** Legs 1-3 wrote in the loop's session, which is still open; leg 5
    # runs in its own transaction and cannot see uncommitted work, so a finalize
    # in the same run reaches the door with the cancels invisible and is refused
    # by its own "still holds live intents" guard. So a run
    # that CHANGED anything schedules the next one and returns; only a run that
    # found nothing left to do, past the window, finalizes.
    # Transit is excluded on purpose — see `reap_transit`: it cannot record
    # its own completion, so counting it here never converges.
    changed = bool(drained["cancelled"] or revoked)
    if changed or not row[2]:  # grace_elapsed
        successor = await _mint_successor(
            session,
            job,
            workspace_id,
            "GREATEST(now(), (SELECT offboarding_at"
            "   + (interval '1 second' * CAST(:g AS bigint))"
            "   FROM workspaces WHERE id = CAST(:ws AS uuid)))",
            {"g": cfg.offboard_grace_seconds},
        )
        return {
            "outcome": "drained" if changed else "grace",
            "successor": successor,
            **report,
        }

    if archive_transit.get("unresolved"):
        # **Never finalize over an upload the provider has not accounted for.**
        # The door would refuse, and a refusal is a raise that spends the job's
        # retry budget: a dead job and a workspace stranded `offboarding` for an
        # upload that may settle in a minute. The run waits instead, as the drain
        # does for publishing work, and says so durably. The successor drains
        # again and finalizes in the run that finds nothing owed; the batch bound
        # (`archive_transit_cleanup.DRAIN_LIMIT`) is what each turn takes.
        await _audit(
            own_tx,
            workspace_id,
            "offboard_archive_transit_unresolved",
            {
                "unresolved": archive_transit["unresolved"],
                "overdue": archive_transit["overdue"],
            },
        )
        # Every turn writes the audit row above, so an overdue upload (one the
        # provider has not accounted for within the review horizon) is asked
        # about once an hour, not at every recheck.
        recheck = cfg.offboard_drain_recheck_seconds
        if archive_transit["overdue"]:
            recheck = max(recheck, OVERDUE_TRANSIT_RECHECK_SECONDS)
        successor = await _mint_successor(
            session, job, workspace_id, _AFTER_RECHECK, {"recheck": recheck}
        )
        return {
            "outcome": "archive_transit_pending",
            "successor": successor,
            **report,
        }
    logger.info(
        "offboard %s: finalizing; the fence warning that follows is this job"
        " observing its own cascade, not another owner winning",
        workspace_id,
    )
    await finalize(own_tx, workspace_id, cfg.offboard_grace_seconds)
    return {"outcome": "finalized", **report}
