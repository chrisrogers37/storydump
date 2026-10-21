"""Drain every admitted past-posts upload in a scope: settle, destroy, verify.

The order a deletion needs once its caller has fenced new admissions (and the
order offboarding's transit leg needs once the workspace is drained):

1. **Settle.** Each `unsettled` or `uncertain` attempt is looked up at the
   provider, outside any transaction. Found: the creation is recorded with
   `instagram_artifacts.record_created`, under the locks the completion owner
   (`instagram_publish.PastPostsPipeline._complete`) takes, a late creation after
   the fence included. Absent: that is absence at that instant only, since a
   held or answer-lost upload can still land, so the attempt stays pending.
2. **Destroy.** Each settled attempt whose cleanup is pending is destroyed
   with the typed `destroy_past_posts`. After settlement, `not_found` is gone.
3. **Verify.** A lookup must then answer absent before `cleanup_state =
   'destroyed'` and `destroyed_at` are written, as a compare-and-set.

Nothing here completes anything: the caller recomputes its predicate from the
database. A provider answer that is an error, or a refusal, leaves the attempt
as it was, and the next run asks again. Attempts are durable rows, so a restart
forgets none. There is never a re-upload and never a drain by assumption:
an upload still unsettled past `DRAIN_REVIEW_AFTER_SECONDS`
is counted `overdue`, for the caller to send to review.

One call takes at most `limit` attempts: those not yet overdue first, then the
oldest. An upload the provider never accounts for stays overdue for good, so a
batch's worth of them must not hide the attempts behind them that can still be
settled. The rest are still owed, so they count in `unresolved` and the caller's
next turn takes them. With no transit store nothing is asked or destroyed: what
the rows still owe is all there is to report, and a scope with no transit
attempts owes nothing.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from src.services.target import instagram_artifacts, instagram_publish, readers
from src.services.target.unit_of_work import unit_of_work

logger = logging.getLogger(__name__)

#: How long an admitted upload may stay unsettled before the caller sends it to
#: review. A past-posts upload is one request inside the egress floor's `upload`
#: class (60 s, one attempt): an hour is sixty of those, so an upload unsettled
#: past it is not plausibly in flight, but nor is it proved absent.
DRAIN_REVIEW_AFTER_SECONDS = 3600

#: The most admitted uploads one call settles and destroys. A scope can hold
#: more (every story a past-posts staged); the rest wait for the next turn, so no
#: call holds a worker for an unbounded number of provider questions.
DRAIN_LIMIT = 50

#: The scope filter: every transit attempt of the workspace unless a source or a
#: generation set narrows it. An empty generation set is an empty scope.
_SCOPE = (
    " AND (CAST(:sid AS uuid) IS NULL OR a.source_id = CAST(:sid AS uuid))"
    " AND (CAST(:gids AS uuid[]) IS NULL"
    " OR a.data_generation_id = ANY(CAST(:gids AS uuid[])))"
)

#: An admitted upload still unsettled past `:horizon` seconds: the one definition
#: of overdue. `owed` counts by it and the drain's batch is ordered by it.
_OVERDUE = (
    "a.outcome IN ('unsettled','uncertain')"
    " AND a.admitted_at <= clock_timestamp() - make_interval(secs => :horizon)"
)

#: The order a bounded batch is taken in: attempts not yet overdue (false sorts
#: first), then the oldest. An overdue attempt is asked again every turn and,
#: where the provider never answers, forever; taken first, a batch's worth of
#: them would hide every later attempt the provider can still settle.
_BATCH_ORDER = " ORDER BY (" + _OVERDUE + "), a.admitted_at, a.artifact_ref"

#: A provider question that raised instead of answering.
_UNANSWERED = object()


@dataclass(frozen=True)
class TransitDrain:
    """One call's work and what the scope still owes after it."""

    #: Attempts this call found created at the provider and recorded settled.
    settled: int
    #: Assets this call proved absent and recorded destroyed.
    destroyed: int
    #: Transit attempts in scope not yet both settled and destroyed.
    unresolved: int
    #: Of those, admitted uploads still unsettled past the review horizon.
    overdue: int


def _params(workspace_id, source_id, generation_ids) -> dict:
    return {
        "ws": str(uuid.UUID(str(workspace_id))),
        "sid": None if source_id is None else str(uuid.UUID(str(source_id))),
        "gids": None
        if generation_ids is None
        else sorted(str(uuid.UUID(str(g))) for g in generation_ids),
    }


def _uow(engine, workspace_id):
    return unit_of_work(engine, workspace_id, actor_kind="system").begin()


async def owed(
    executor, *, workspace_id, source_id=None, generation_ids=None
) -> tuple[int, int]:
    """`(unresolved, overdue)` for a scope, from the rows and the database clock."""
    found = await readers.row(
        executor,
        "SELECT count(*) FILTER (WHERE NOT (a.outcome = 'settled'"
        " AND a.cleanup_state = 'destroyed')) AS unresolved,"
        " count(*) FILTER (WHERE " + _OVERDUE + ")"
        " AS overdue FROM instagram_artifacts a"
        " WHERE a.workspace_id = :ws AND a.kind = 'transit'" + _SCOPE,
        horizon=DRAIN_REVIEW_AFTER_SECONDS,
        **_params(workspace_id, source_id, generation_ids),
    )
    return int(found["unresolved"]), int(found["overdue"])


async def _ask(call, ref, resource_type, what):
    """One provider question; an error answers nothing and changes nothing."""
    try:
        return await call(ref, resource_type=resource_type)
    except Exception as exc:  # noqa: BLE001 — no provider error proves an outcome
        logger.warning(
            "past-posts transit drain: the %s of %s raised %s; the attempt stays as it was",
            what,
            ref,
            type(exc).__name__,
        )
        return _UNANSWERED


async def settle_and_destroy(
    engine,
    *,
    workspace_id,
    transit,
    source_id=None,
    generation_ids=None,
    limit=DRAIN_LIMIT,
) -> TransitDrain:
    """Settle, destroy and verify the scope's admitted uploads; never complete.

    *transit* is the `TransitStore` (its typed `past_posts_lookup` and
    `destroy_past_posts`), or None where none is configured. Call with no unit of
    work open: every provider call is made between this function's own short
    transactions. At most *limit* attempts are taken, those not yet overdue
    first and then oldest first; the others stay owed and are counted
    unresolved.
    """
    params = _params(workspace_id, source_id, generation_ids)
    ws = params["ws"]
    attempts = []
    if transit is not None:
        async with _uow(engine, ws) as conn:
            attempts = await readers.rows(
                conn,
                "SELECT a.artifact_ref, a.intent_id, a.outcome, a.admitted_asset_ref,"
                " a.admitted_resource_type FROM instagram_artifacts a"
                " WHERE a.workspace_id = :ws AND a.kind = 'transit'"
                " AND a.admitted_asset_ref IS NOT NULL"
                " AND (a.outcome IN ('unsettled','uncertain') OR a.cleanup_state = 'pending')"
                + _SCOPE
                + _BATCH_ORDER
                + " LIMIT :lim",
                lim=int(limit),
                horizon=DRAIN_REVIEW_AFTER_SECONDS,
                **params,
            )
    settled = destroyed = 0
    for attempt in attempts:
        artifact_ref, intent_id = (
            str(attempt["artifact_ref"]),
            str(attempt["intent_id"]),
        )
        ref, resource_type = (
            attempt["admitted_asset_ref"],
            attempt["admitted_resource_type"],
        )
        if attempt["outcome"] != "settled":
            evidence = await _ask(
                transit.past_posts_lookup, ref, resource_type, "lookup"
            )
            if evidence is None or evidence is _UNANSWERED:
                continue
            async with _uow(engine, ws) as conn:
                await instagram_publish.lock_pipeline(
                    conn, workspace_id=ws, intent_id=intent_id, effects=False
                )
                await instagram_artifacts.record_created(
                    conn, workspace_id=ws, artifact_ref=artifact_ref, evidence=evidence
                )
            settled += 1
        if (
            await _ask(transit.destroy_past_posts, ref, resource_type, "destroy")
            is _UNANSWERED
        ):
            continue
        # Still there, or no answer: neither is absence.
        if (
            await _ask(transit.past_posts_lookup, ref, resource_type, "verification")
            is not None
        ):
            continue
        async with _uow(engine, ws) as conn:
            await instagram_publish.lock_pipeline(
                conn, workspace_id=ws, intent_id=intent_id, effects=False
            )
            moved = await instagram_artifacts.mark_destroyed(
                conn, workspace_id=ws, artifact_ref=artifact_ref
            )
        destroyed += moved
    async with _uow(engine, ws) as conn:
        unresolved, overdue = await owed(
            conn, workspace_id=ws, source_id=source_id, generation_ids=generation_ids
        )
    return TransitDrain(settled, destroyed, unresolved, overdue)
