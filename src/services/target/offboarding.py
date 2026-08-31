"""The `offboard_workspace` job — `06` §1's five legs, orchestrated.

`04` X.3 is explicit that this is the piece to build: *"the `offboard_workspace`
executor itself — the orchestration of `06` §1's legs (drain → revoke → transit
reap → grace → `fn_offboard_finalize`), built here explicitly, not just the
command that enqueues it"* (R3 §3.5 flagged it as named-but-never-built). The
door and the job kind have existed since `059` and `056`; what was missing was
the thing that calls them in order.

## Leg order is load-bearing, and it is not the obvious one

`06` §1 records that the pass-2 ordering revoked credentials **before** draining
publishing work, which destroyed exactly the credentials reconciliation still
needed to find out whether a post went out. So: drain first, with credentials
alive; revoke second; transit reap third; then the grace window; then deletion.
A drain that does not finish inside `05`'s timeout **parks rather than
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

## Two interactions with the work loop that had to be designed around

**The finalize leg deletes its own job row.** `jobs.workspace_id` is
``REFERENCES workspaces(id) ON DELETE CASCADE`` (`056:91`), so the moment
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

## What is inert in v1, said rather than left to be discovered

Legs 1 and 3 have nothing to act on in a manual-mode workspace, and that is a
property of the data rather than of this code. `publishing` and
`publishing_ambiguous` are reachable only through the publish pipeline, whose
only producer in the entire codebase is `approve` (`command_executors.py:130`),
which refuses with `manual_mode` unless `api_publishing_enabled`; the same gate
means no intent carries a `transit_asset_ref`. Both legs are built to `06` §1
anyway: milestone 2 populates them, and a leg written later against a schema
already carrying rows is the harder version of this job.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from sqlalchemy import text

from src.services.target import intent_ledger, jobs
from src.services.target.intent_ledger import IntentTransitionRefused

logger = logging.getLogger(__name__)

#: `02` §5 registry — lane and serialization key for the kind.
LANE = "bulk"

#: `02` §4 terminal intent states. A cancel is legal from every WORKING state
#: except the two publishing ones, which must drain rather than be flipped.
TERMINAL_STATES = ("posted", "skipped", "rejected", "expired", "failed", "cancelled")
DRAINING_STATES = ("publishing", "publishing_ambiguous")


def serialization_key(workspace_id: str) -> str:
    return f"ws:{workspace_id}"


class DrainTimedOut(Exception):
    """`06` §1 leg 1's park: live publishing work outlived `05`'s drain
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


async def drain(session, workspace_id: str) -> dict[str, Any]:
    """Leg 1 — terminalize every live intent, with credentials still alive.

    The cancel goes through `intent_ledger.transition` rather than a bulk
    UPDATE so `trg_intent_guard` rules on each edge; `06` §1 says cancel is
    "legal from every working state except publishing/ambiguous" and the guard
    is where that is actually enforced. A refusal on one intent is recorded and
    the rest continue — one unexpected edge must not strand the whole offboard.
    """
    rows = (
        await session.execute(
            text(
                "SELECT id::text FROM post_intents"
                " WHERE workspace_id = :ws"
                "   AND NOT (state = ANY(:terminal))"
                "   AND NOT (state = ANY(:draining))"
                " ORDER BY id"
            ),
            {
                "ws": workspace_id,
                "terminal": list(TERMINAL_STATES),
                "draining": list(DRAINING_STATES),
            },
        )
    ).all()
    cancelled, refused = 0, 0
    for (intent_id,) in rows:
        try:
            await intent_ledger.transition(session, intent_id, "cancelled")
            cancelled += 1
        except IntentTransitionRefused:
            logger.warning(
                "offboard drain: intent %s refused the cancel edge; leaving it"
                " for the finalize guard to catch",
                intent_id,
            )
            refused += 1
    return {"cancelled": cancelled, "refused": refused}


async def revoke_credentials(session, workspace_id: str) -> int:
    """Leg 2 — every credential goes `revoked` here, and the best-effort
    provider call rides its own job (`#1083`'s `revoke_workspace_credentials`,
    already built). `06` §1: a revocation that still fails is recorded and
    abandoned, because the row dies with the workspace and the provider token
    expires on its own.
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


async def reap_transit(session, workspace_id: str, *, destroy) -> dict[str, Any]:
    """Leg 3 — destroy this workspace's live transit assets (FC-3.5).

    *destroy* is the injected `deps.transit.destroy_asset`, or None when no
    transit store is configured. With none, the refs are LEFT and reported:
    `06` §1 names the FC-3.6 TTL sweep as the backstop and the assets are
    TTL-bounded in minutes regardless, so a missing seam delays nothing that
    the offboard is responsible for. Reporting rather than silently passing is
    the difference between a documented backstop and an unnoticed skip.

    **The ref column is not cleared, and cannot be.** Leg 1 has already
    terminalized every intent and `trg_intent_terminal_freeze` makes a terminal
    row immutable — the first version of this leg NULLed the ref after a
    successful destroy and the gate answered "post_intent … is terminal
    (cancelled) and immutable". Nor does it need clearing: the row dies at leg
    5 either way, and the only thing the column buys after that is the handle
    itself. The cost is that a re-run re-attempts a destroy the provider has
    already done, which is one no-op call per asset on the normal two-run path
    and is why this leg is deliberately excluded from the caller's "did this run
    change anything" test — counting it would schedule a successor forever.
    """
    rows = (
        await session.execute(
            text(
                "SELECT id::text, transit_asset_ref FROM post_intents"
                " WHERE workspace_id = :ws AND transit_asset_ref IS NOT NULL"
            ),
            {"ws": workspace_id},
        )
    ).all()
    if destroy is None:
        return {"reaped": 0, "left_to_ttl": len(rows), "seam": "absent"}
    reaped, failed = 0, 0
    for _intent_id, ref in rows:
        try:
            await destroy(ref)
        except Exception:  # noqa: BLE001 — best-effort; the TTL sweep backstops
            logger.warning("offboard transit reap: %s refused; left to TTL", ref)
            failed += 1
            continue
        reaped += 1
    return {"reaped": reaped, "left_to_ttl": failed, "seam": "wired"}


async def _audit(factory, workspace_id: str, event: str, detail: dict) -> None:
    """A parked leg writes no row of its own — `workspaces` carries
    `trg_governance_audit` only for UPDATEs to itself, and a drain that stalls
    updates nothing — so it writes one here.

    **In its OWN transaction, which is the whole point.** The caller that needs
    this raises immediately afterwards, and a record written in the raising
    transaction is rolled back with it: the durable signal for a parked drain
    would be exactly as durable as no signal at all. Same reason
    `credential_lifecycle._audit_revoke_failed` opens its own.
    """
    async with factory() as session:
        await session.execute(
            text(
                "INSERT INTO audit_events (workspace_id, entity_kind, entity_id,"
                " from_state, to_state, actor_kind, actor_user_id, channel, detail)"
                " VALUES (:ws, 'workspace', CAST(:ws AS uuid), 'offboarding',"
                "         'offboarding', current_setting('app.actor_kind'),"
                "         NULL, 'system', CAST(:detail AS jsonb))"
            ),
            {
                "ws": workspace_id,
                "detail": json.dumps({"v": 1, "event": event, **detail}),
            },
        )
        await session.commit()


async def _mint_successor(session, job, workspace_id: str, run_at_sql: str, params: dict) -> Optional[str]:
    """The next run of this workflow. `jobs.enqueue` cannot express a future
    `run_at` (it hardcodes `now()`, which is right for every other producer),
    and `unless_pending` cannot be used at all here because THIS job holds the
    same serialization key, so the insert and its duplicate guard are written
    out. The guard excludes the current row by id — without that exclusion it
    would never mint, and with no guard at all a re-claimed lease would mint a
    second workflow for one workspace."""
    key = serialization_key(workspace_id)
    row = (
        await session.execute(
            text(
                "INSERT INTO jobs (kind, workspace_id, lane, serialization_key,"
                " run_at, max_attempts, payload)"
                " SELECT 'offboard_workspace', CAST(:ws AS uuid), CAST(:lane AS text),"
                f"        CAST(:key AS text), {run_at_sql}, 5,"
                "        CAST(:p AS jsonb)"
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
    """`06` §1's five legs. Returns what it did, for the loop's log.

    *session* is the loop's transaction: legs 1–3 and the successor mint ride
    it. Leg 5 does not, and the module docstring says why.
    """
    from src.services.target.work_loop import poller_session_factory

    cfg = deps.config
    workspace_id = str(job["workspace_id"])
    row = (
        await session.execute(
            text("SELECT state, offboarding_at FROM workspaces WHERE id = :ws"),
            {"ws": workspace_id},
        )
    ).first()
    if row is None:
        # Already finalized — a duplicate or a re-claimed lease arriving after
        # the cascade. Nothing to do and nothing wrong.
        return {"outcome": "already_finalized"}
    if row[0] != "offboarding":
        # Restored inside the grace window (`06` §1), or never offboarding.
        # The workflow stops; it does not un-cancel anything, which is exactly
        # what `06` §1's restore semantics say (state + mandatory reconnect).
        return {"outcome": "not_offboarding", "state": row[0]}

    drained = await drain(session, workspace_id)
    still_publishing = await _live_publishing(session, workspace_id)
    if still_publishing:
        expired = (
            await session.execute(
                text(
                    "SELECT offboarding_at + (interval '1 second' * CAST(:d AS bigint))"
                    "       <= now() FROM workspaces WHERE id = :ws"
                ),
                {"ws": workspace_id, "d": cfg.offboard_drain_timeout_seconds},
            )
        ).scalar()
        if expired:
            # `06` §1: park, do not revoke under live work. There is no operator
            # alert channel in this tier — `06` §5's producers are unbuilt and
            # the notification route has no writer — so the loudest durable
            # signals available are used: an audit row, and a raise that spends
            # the job's retry budget and lands as a dead job row.
            await _audit(
                poller_session_factory(deps.engine, workspace_id),
                workspace_id,
                "offboard_drain_timeout",
                {"publishing": still_publishing},
            )
            raise DrainTimedOut(
                f"workspace {workspace_id}: {still_publishing} intent(s) still"
                f" publishing past the {cfg.offboard_drain_timeout_seconds}s"
                " drain timeout; not revoking under live work"
            )
        successor = await _mint_successor(
            session,
            job,
            workspace_id,
            "now() + (interval '1 second' * CAST(:recheck AS bigint))",
            {"recheck": cfg.offboard_drain_recheck_seconds},
        )
        return {
            "outcome": "draining",
            "publishing": still_publishing,
            "successor": successor,
            **drained,
        }

    revoked = await revoke_credentials(session, workspace_id)
    transit = await reap_transit(
        session,
        workspace_id,
        destroy=getattr(deps.transit, "destroy_asset", None),
    )

    elapsed = (
        await session.execute(
            text(
                "SELECT offboarding_at + (interval '1 second' * CAST(:g AS bigint))"
                "       <= now() FROM workspaces WHERE id = :ws"
            ),
            {"ws": workspace_id, "g": cfg.offboard_grace_seconds},
        )
    ).scalar()

    # **The finalizer is always a LATER run, even when the window has already
    # closed.** Legs 1-3 wrote in the loop's session, which is still open; leg 5
    # runs in its own transaction and cannot see uncommitted work, so a finalize
    # in the same run reaches the door with the cancels invisible and is refused
    # by its own "still holds live intents" guard. Measured, not anticipated —
    # the first version did exactly that and the gate failed on it. So a run
    # that CHANGED anything schedules the next one and returns; only a run that
    # found nothing left to do, past the window, finalizes.
    # Transit is excluded on purpose — see `reap_transit`: it cannot record
    # its own completion, so counting it here never converges.
    changed = bool(drained["cancelled"] or revoked)
    if changed or not elapsed:
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
            "revoked": revoked,
            "transit": transit,
            "successor": successor,
            **drained,
        }

    logger.info(
        "offboard %s: finalizing; the fence warning that follows is this job"
        " observing its own cascade, not another owner winning",
        workspace_id,
    )
    await finalize(
        poller_session_factory(deps.engine, workspace_id),
        workspace_id,
        cfg.offboard_grace_seconds,
    )
    return {"outcome": "finalized", "revoked": revoked, "transit": transit, **drained}
