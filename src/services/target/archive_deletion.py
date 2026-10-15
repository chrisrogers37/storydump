"""Deletion cleanup: one Meta deletion receipt's scopes in one workspace.

`execute(deps, job)` is the `meta_deletion_cleanup` executor (#1644).
It owns its transactions (register it under `work_loop.own_transactions`, with
no session open), because the transit drain asks the provider between them,
never inside one. Each step is one tenant unit of work that takes the canonical
source prefix first and asserts the job's lease last:

A. **Fence.** A held scope's generations go to `review_hold`: unavailable, not
   destroyed. An in-scope scope's go to `deletion_pending`. The live intents
   bound to them are flagged `cancel_requested` and their
   cards lose their buttons, a sync checkpoint carrying them is cleared, and a
   pending scope reports `cleaning`. The publishing credential is not revoked:
   a deletion request is not a deauthorization.
B. **Drain transit** with `archive_transit_cleanup.settle_and_destroy`, a bounded
   batch per source part. Without a transit store it asks and destroys nothing,
   and any upload the scope still holds counts as unresolved.
C. **Redact, all or nothing.** `fn_archive_redact_generation_records` deletes
   the provenance and artifact records, and refuses by name while anything can
   still use them. Then the bound intents' outbox payloads go, but only once
   those intents are terminal and none of their payloads is still owed
   delivery. The observations and the shared item fields go next (tombstoned),
   and the generations close `deleted`.
D. **Recount** every category of the completion predicate from the database and
   report it through `fn_meta_receipt_progress`. Only a zero in every category
   completes the scope; a successful call completes nothing.

Run outcomes, logged and returned: `held`, `cleaning`, `transit_unresolved`,
`operation_unresolved`, `intent_live`, `completed`. `fenced` is logged when a
run fences generations, and it is the recorded result a scope keeps if the run
dies between A and D. An admitted upload still unsettled past the drain horizon
sends the receipt to review as `transit_unresolved`; nothing re-uploads.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass

from asyncpg.exceptions import RaiseError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.exceptions.base import RefusalError
from src.services.target import (
    archive_transit_cleanup,
    instagram_catalog,
    intent_ledger,
    jobs,
    outbox,
    prompts,
    readers,
    source_membership,
    source_state,
)
from src.services.target._dbapi import driver_error_is
from src.services.target.unit_of_work import unit_of_work
from src.utils.datetime_utils import utcnow

logger = logging.getLogger(__name__)

#: Seconds until a scope still cleaning is due again (the door clamps to 60..3600).
RETRY_AFTER_SECONDS = 60
#: The same once an overdue upload has sent the receipt to review: only a late
#: creation can still move the scope, and nothing resolves the review.
REVIEW_RETRY_AFTER_SECONDS = 3600

#: The completion predicate's categories, as a scope's results report them.
#: File validation evidence rides asset rows; the recount names both bundles.
CATEGORIES = (
    "observations",
    "assets",
    "shared_fields",
    "intent_provenance",
    "operation_provenance",
    "artifacts",
    "transit",
    "transit_overdue",
    "operations",
    "live_intents",
    "payload_snapshots",
    "checkpoints",
    "generations",
)

#: The categories `_REMAINING` counts: all but the transit pair, which
#: `archive_transit_cleanup.owed` counts.
_COUNTED = tuple(c for c in CATEGORIES if c not in ("transit", "transit_overdue"))

#: The redaction door's refusals a run reports as its outcome. `scope_unavailable`
#: is not one: after the fence it means a broken invariant, so it propagates.
_REDACTION_REFUSAL = re.compile(
    r"archive redaction refused: (operation_unresolved|transit_unresolved|intent_live)\b"
)

#: The intents bound to one source's frozen generations (`:ws`, `:sid`, `:gids`):
#: captured or staged from them, or, before preparation records provenance,
#: minted for an item whose shared projection they still own. A row linked only
#: by its selection generation is bound to the capture that replaced it.
_BOUND = (
    "SELECT bp.intent_id FROM instagram_intent_provenance bp"
    " WHERE bp.workspace_id = :ws AND bp.source_id = :sid"
    " AND (bp.capture_generation_id = ANY(CAST(:gids AS uuid[]))"
    " OR bp.staged_generation_id = ANY(CAST(:gids AS uuid[])))"
    " UNION SELECT bi.id FROM post_intents bi JOIN media_items bm"
    " ON bm.workspace_id = bi.workspace_id AND bm.id = bi.media_item_id"
    " WHERE bi.workspace_id = :ws AND bm.source_id = :sid"
    " AND bm.source_data_generation_id = ANY(CAST(:gids AS uuid[]))"
    " AND NOT EXISTS (SELECT 1 FROM instagram_intent_provenance bq"
    " WHERE bq.workspace_id = bi.workspace_id AND bq.intent_id = bi.id)"
)

#: `provisioning.disable_destination`'s flag, over the bound intents.
_FLAG = (
    "UPDATE post_intents SET cancel_requested = true"
    " WHERE workspace_id = :ws AND id IN (" + _BOUND + ")"
    f" AND NOT cancel_requested AND {intent_ledger.NOT_TERMINAL}"
    " RETURNING id, state"
)

#: A checkpoint names its generation and carries provider ids (pending parents).
_CLEAR_CHECKPOINT = (
    "UPDATE media_sources SET sync_checkpoint = NULL"
    " WHERE workspace_id = :ws AND id = :sid AND provider = 'instagram_archive'"
    " AND sync_checkpoint->>'generation_id' = ANY(CAST(CAST(:gids AS uuid[]) AS text[]))"
)

#: What C needs to know of each bound intent before it may redact.
_BOUND_STATE = (
    "SELECT i.id, i.state <> ALL(CAST(:terminal AS text[])) AS live,"
    " EXISTS (SELECT 1 FROM channel_outbox o WHERE o.workspace_id = i.workspace_id"
    " AND o.intent_id = i.id AND o.state IN ('pending','sending','ambiguous')) AS owed"
    " FROM post_intents i WHERE i.workspace_id = :ws AND i.id IN (" + _BOUND + ")"
)

#: What is left in each of `_COUNTED`'s categories, one alias apiece. `_report`
#: reads each by name, so an alias this statement lacks fails the recount rather
#: than reading as nothing left.
_REMAINING = (
    "WITH bound AS (" + _BOUND + ")"
    " SELECT (SELECT count(*) FROM instagram_media_index x WHERE x.workspace_id = :ws"
    " AND x.source_id = :sid AND x.data_generation_id = ANY(CAST(:gids AS uuid[])))"
    " AS observations,"
    " (SELECT count(*) FROM instagram_media_assets a WHERE a.workspace_id = :ws"
    " AND a.source_id = :sid AND a.data_generation_id = ANY(CAST(:gids AS uuid[])))"
    " AS assets,"
    " (SELECT count(*) FROM media_items m WHERE m.workspace_id = :ws AND m.source_id = :sid"
    " AND m.source_data_generation_id = ANY(CAST(:gids AS uuid[]))) AS shared_fields,"
    " (SELECT count(*) FROM instagram_intent_provenance p WHERE p.workspace_id = :ws"
    " AND p.source_id = :sid AND (p.capture_generation_id = ANY(CAST(:gids AS uuid[]))"
    " OR p.staged_generation_id = ANY(CAST(:gids AS uuid[])))) AS intent_provenance,"
    " (SELECT count(*) FROM instagram_operation_provenance p WHERE p.workspace_id = :ws"
    " AND p.source_id = :sid AND p.data_generation_id = ANY(CAST(:gids AS uuid[])))"
    " AS operation_provenance,"
    " (SELECT count(*) FROM instagram_artifacts a WHERE a.workspace_id = :ws"
    " AND a.source_id = :sid AND a.data_generation_id = ANY(CAST(:gids AS uuid[])))"
    " AS artifacts,"
    " (SELECT count(*) FROM provider_operations o JOIN instagram_operation_provenance p"
    " ON p.workspace_id = o.workspace_id AND p.operation_id = o.id"
    " WHERE p.workspace_id = :ws AND p.source_id = :sid"
    " AND p.data_generation_id = ANY(CAST(:gids AS uuid[]))"
    " AND o.state IN ('permitted','ambiguous')) AS operations,"
    " (SELECT count(*) FROM post_intents i WHERE i.workspace_id = :ws"
    " AND i.id IN (SELECT intent_id FROM bound)"
    " AND i.state <> ALL(CAST(:terminal AS text[]))) AS live_intents,"
    " (SELECT count(*) FROM channel_outbox o WHERE o.workspace_id = :ws"
    " AND o.intent_id IN (SELECT intent_id FROM bound)"
    " AND o.payload->>'redacted' IS DISTINCT FROM 'true') AS payload_snapshots,"
    " (SELECT count(*) FROM media_sources s WHERE s.workspace_id = :ws AND s.id = :sid"
    " AND s.sync_checkpoint->>'generation_id'"
    " = ANY(CAST(CAST(:gids AS uuid[]) AS text[]))) AS checkpoints,"
    " (SELECT count(*) FROM instagram_archive_generations g WHERE g.workspace_id = :ws"
    " AND g.source_id = :sid AND g.id = ANY(CAST(:gids AS uuid[]))"
    " AND g.lifecycle <> 'deleted') AS generations"
)


class DeletionCleanupRefused(RefusalError):
    """payload_invalid: the job does not name a workspace and a receipt."""

    _prefix = "deletion cleanup refused"


@dataclass(frozen=True)
class _Scope:
    """One frozen scope of the receipt in this workspace."""

    credential_id: str
    authorization_id: str
    disposition: str
    state: str
    #: (source id, frozen generation ids) per source; empty when nothing froze.
    parts: tuple[tuple[str, frozenset[str]], ...]


class _Blocked(Exception):
    """Rolls C's transaction back without a door refusal: the outcome it names."""

    def __init__(self, outcome: str):
        super().__init__(outcome)
        self.outcome = outcome


def _subject(job) -> tuple[str, str]:
    payload = job.get("payload") or {}
    try:
        if payload.get("v") != 1:
            raise ValueError("payload version")
        return (
            str(uuid.UUID(str(job["workspace_id"]))),
            str(uuid.UUID(str(payload["receipt_id"]))),
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        raise DeletionCleanupRefused("payload_invalid") from None


def _uow(engine, workspace_id):
    return unit_of_work(engine, workspace_id, actor_kind="system").begin()


def _params(ws, source_id, generation_ids) -> dict:
    return {"ws": ws, "sid": source_id, "gids": sorted(generation_ids)}


async def _scopes(conn, receipt) -> list[_Scope]:
    rows = await readers.rows(
        conn,
        "SELECT o_credential_id, o_authorization_id, o_disposition, o_state,"
        " o_source_id, o_generation_ids FROM fn_meta_receipt_scope(CAST(:receipt AS uuid))",
        receipt=receipt,
    )
    grouped: dict[tuple[str, str], tuple[str, str, list]] = {}
    for r in rows:
        key = (str(r["o_credential_id"]), str(r["o_authorization_id"]))
        _, _, parts = grouped.setdefault(key, (r["o_disposition"], r["o_state"], []))
        if r["o_source_id"] is not None and r["o_generation_ids"]:
            gids = frozenset(str(g) for g in r["o_generation_ids"])
            parts.append((str(r["o_source_id"]), gids))
    return [
        _Scope(credential, authorization, disposition, state, tuple(parts))
        for (credential, authorization), (disposition, state, parts) in grouped.items()
    ]


async def _progress(conn, receipt, scope, state, results, reason, retry_after):
    return await conn.scalar(
        text(
            "SELECT fn_meta_receipt_progress(CAST(:receipt AS uuid),"
            " CAST(:credential AS uuid), CAST(:authorization AS uuid),"
            " CAST(:state AS text), CAST(:results AS jsonb), CAST(:reason AS text),"
            " CAST(:retry AS integer))"
        ),
        {
            "receipt": receipt,
            "credential": scope.credential_id,
            "authorization": scope.authorization_id,
            "state": state,
            "results": json.dumps(results, sort_keys=True),
            "reason": reason,
            "retry": retry_after,
        },
    )


async def _flag_bound(conn, params) -> None:
    """As `disable_account` does it: flag, then strip the live cards. A
    parked story keeps its review card: giving it up is its resolution."""
    flagged = await readers.rows(conn, _FLAG, **params)
    carded = [str(r["id"]) for r in flagged if r["state"] != "review_required"]
    if carded:
        zones = await readers.rows(
            conn,
            "SELECT i.id, COALESCE(a.tz, w.tz) AS eff_tz FROM post_intents i"
            " JOIN workspaces w ON w.id = i.workspace_id"
            " JOIN ig_accounts a ON a.id = i.ig_account_id"
            " WHERE i.workspace_id = :ws AND i.id = ANY(CAST(:ids AS uuid[]))",
            ws=params["ws"],
            ids=carded,
        )
        for zone in zones:
            line = prompts.outcome_line(
                "cancelled", by=None, at=utcnow(), tz=str(zone["eff_tz"] or "UTC")
            )
            await outbox.supersede_everywhere(
                conn,
                workspace_id=params["ws"],
                intent_id=str(zone["id"]),
                outcome_text=line,
            )


async def _fence(engine, ws, receipt, job) -> tuple[list[_Scope], int]:
    """A, for every scope of the receipt in this workspace, held ones included."""
    fenced = 0
    async with _uow(engine, ws) as conn:
        await source_state.lock_all_workspace_sources(conn, workspace_id=ws)
        scopes = await _scopes(conn, receipt)
        for scope in scopes:
            if scope.state not in ("pending", "cleaning", "held"):
                continue
            held = scope.disposition == "held"
            for source_id, generation_ids in scope.parts:
                result = await source_membership.fence_generations(
                    conn,
                    workspace_id=ws,
                    source_id=source_id,
                    generation_ids=generation_ids,
                    reason="review" if held else "deletion",
                )
                fenced += len(result.changed_generation_ids)
                if not held:
                    params = _params(ws, source_id, generation_ids)
                    await _flag_bound(conn, params)
                    await conn.execute(text(_CLEAR_CHECKPOINT), params)
            if not held and scope.state == "pending":
                await _progress(
                    conn,
                    receipt,
                    scope,
                    "cleaning",
                    {"v": 1, "outcome": "fenced"},
                    None,
                    RETRY_AFTER_SECONDS,
                )
        await jobs.assert_lease(conn, job["id"], job["lease_token"])
    return scopes, fenced


async def _redact(engine, ws, scope, keys, job) -> str | None:
    """C for one scope: None once every part is redacted, else what blocks it."""
    for source_id, generation_ids in scope.parts:
        params = _params(ws, source_id, generation_ids)
        try:
            async with _uow(engine, ws) as conn:
                await source_state.lock_all_workspace_sources(conn, workspace_id=ws)
                bound = await readers.rows(
                    conn,
                    _BOUND_STATE,
                    terminal=list(intent_ledger.TERMINAL_STATES),
                    **params,
                )
                await conn.execute(
                    text(
                        "SELECT o_operation_rows, o_intent_rows, o_artifact_rows FROM"
                        " fn_archive_redact_generation_records(CAST(:sid AS uuid),"
                        " CAST(:gids AS uuid[]))"
                    ),
                    params,
                )
                # The door saw every intent with provenance; one without is still
                # a live story about this content.
                if any(b["live"] for b in bound):
                    raise _Blocked("intent_live")
                # A queued edit still needs its routing; it is redacted once sent.
                if any(b["owed"] for b in bound):
                    raise _Blocked("cleaning")
                if bound:
                    await conn.execute(
                        text(
                            "UPDATE channel_outbox SET payload = jsonb_build_object("
                            "'v', 1, 'redacted', true, 'kind', kind)"
                            " WHERE workspace_id = :ws"
                            " AND intent_id = ANY(CAST(:ids AS uuid[]))"
                            " AND payload->>'redacted' IS DISTINCT FROM 'true'"
                        ),
                        {"ws": ws, "ids": [str(b["id"]) for b in bound]},
                    )
                await instagram_catalog.redact_generations(
                    conn,
                    workspace_id=ws,
                    source_id=source_id,
                    generation_ids=generation_ids,
                    keys=keys,
                )
                await source_membership.mark_generations_deleted(
                    conn,
                    workspace_id=ws,
                    source_id=source_id,
                    generation_ids=generation_ids,
                )
                await jobs.assert_lease(conn, job["id"], job["lease_token"])
        except _Blocked as blocked:
            return blocked.outcome
        except DBAPIError as exc:
            refusal = driver_error_is(exc, RaiseError)
            named = _REDACTION_REFUSAL.search(str(refusal)) if refusal else None
            if named is None:
                raise
            return named.group(1)
    return None


async def _report(engine, ws, receipt, scope, drains, blocker, job) -> str:
    """D: recount, then report. The scope completes only on zero everywhere."""
    counts = dict.fromkeys(CATEGORIES, 0)
    async with _uow(engine, ws) as conn:
        await source_state.lock_all_workspace_sources(conn, workspace_id=ws)
        for source_id, generation_ids in scope.parts:
            params = _params(ws, source_id, generation_ids)
            remaining = await readers.row(
                conn, _REMAINING, terminal=list(intent_ledger.TERMINAL_STATES), **params
            )
            transit, overdue = await archive_transit_cleanup.owed(
                conn,
                workspace_id=ws,
                source_id=source_id,
                generation_ids=generation_ids,
            )
            for category in _COUNTED:
                counts[category] += int(remaining[category])
            counts["transit"] += transit
            counts["transit_overdue"] += overdue
        done = not any(counts.values())
        outcome = "completed" if done else (blocker or "cleaning")
        review = counts["transit_overdue"] > 0
        state = await _progress(
            conn,
            receipt,
            scope,
            "completed" if done else "cleaning",
            {
                "v": 1,
                "outcome": outcome,
                "transit_settled": sum(d.settled for d in drains),
                "transit_destroyed": sum(d.destroyed for d in drains),
                **counts,
            },
            "transit_unresolved" if review else None,
            REVIEW_RETRY_AFTER_SECONDS if review else RETRY_AFTER_SECONDS,
        )
        await jobs.assert_lease(conn, job["id"], job["lease_token"])
    logger.info(
        "meta_deletion_cleanup %s: receipt %s scope %s -> %s (receipt %s)",
        job["id"],
        receipt,
        scope.authorization_id,
        outcome,
        state,
    )
    return outcome


async def execute(deps, job) -> str:
    """One run for one (receipt, workspace): A for every scope, then B-D per
    in-scope scope. Needs `deps.engine`, `deps.archive_keys` (the tombstone key
    ring) and `deps.transit` (the transit store), which may be None: the scope
    then completes only if it holds no unresolved upload."""
    ws, receipt = _subject(job)
    scopes, fenced = await _fence(deps.engine, ws, receipt, job)
    if fenced:
        logger.info(
            "meta_deletion_cleanup %s: receipt %s fenced %d generation(s)",
            job["id"],
            receipt,
            fenced,
        )
    outcomes = []
    for scope in scopes:
        if scope.state == "workspace_purged":
            continue
        if scope.state == "completed":
            outcomes.append("completed")
            continue
        if scope.disposition == "held":
            outcomes.append("held")
            continue
        drains = [
            await archive_transit_cleanup.settle_and_destroy(
                deps.engine,
                workspace_id=ws,
                transit=deps.transit,
                source_id=source_id,
                generation_ids=generation_ids,
                limit=archive_transit_cleanup.DRAIN_LIMIT,
            )
            for source_id, generation_ids in scope.parts
        ]
        blocker = await _redact(deps.engine, ws, scope, deps.archive_keys, job)
        outcomes.append(
            await _report(deps.engine, ws, receipt, scope, drains, blocker, job)
        )
    return ",".join(sorted(set(outcomes))) or "completed"
