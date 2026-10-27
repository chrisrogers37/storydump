"""Captured past-posts turns with one atomic metadata/checkpoint/job commit.

Composition is explicitly injected. Provider metadata reads happen only after
short source/lease checks have closed; the database remains the sole commit
and suitability authority. Media bytes/probes belong after candidate selection,
never to indexing. Only `past_posts_composition` constructs these ports, and only
when `past_posts_activation` arms the process.
"""

from __future__ import annotations

import copy
import json
import logging
import math
from datetime import datetime
from dataclasses import dataclass
from typing import Awaitable, Callable, Any

from sqlalchemy import text

from src.services.target import instagram_catalog, jobs, readers, source_policy
from src.services.target import source_read_admission as admission
from src.services.target.instagram_sources import PastPostsCapability, is_activated
from src.services.target.media_sync import BASELINE_SECONDS
from src.services.target.instagram_past_posts_adapter import (
    LAUNCH_FLOOR,
    LISTING_CEILING,
    MAX_PAGE_REQUESTS,
    WINDOW_SECONDS,
)
from src.services.target.media_adapters import (
    MediaPage,
    MediaTurnCounters,
    MediaSourceFenceDrop,
    MediaTerminalError,
    MediaCredentialDead,
    MediaSourceGone,
    redacted_error,
    MEDIA_FAILURES,
)
from src.services.target.unit_of_work import unit_of_work

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PastPostsSyncPorts:
    capability: PastPostsCapability
    tombstone_keys: instagram_catalog.TombstoneKeys
    prepare_adapter: Callable[[admission.SourceReadCapture, Any], Awaitable[Any]]


@dataclass(frozen=True)
class SyncTurnResult:
    outcome: str
    lane: str | None = None
    committed_observations: int = 0
    graph_attempts: int = 0
    retry_count: int = 0
    drop_reason: str | None = None
    cdn_attempts: int = 0


def _counters(value):
    return value if isinstance(value, MediaTurnCounters) else MediaTurnCounters()


def _retry_count(job):
    return max(0, int(job.get("attempts", 1)) - 1)


def _attempt_result(
    outcome, lane, *, job, drop_reason=None, page=None, error=None, committed=0
):
    counters = _counters(
        page.counters if page is not None else getattr(error, "counters", None)
    )
    return SyncTurnResult(
        outcome,
        lane,
        committed_observations=committed,
        graph_attempts=counters.graph_attempts,
        cdn_attempts=counters.cdn_attempts,
        retry_count=_retry_count(job),
        drop_reason=drop_reason,
    )


def _fresh_lane():
    return {"checkpoint": {"v": 2}, "complete": False}


def _clamp_ceiling(checkpoint):
    checkpoint.update(seen=LISTING_CEILING, more=False, ceiling_reached=True)
    checkpoint.pop("pending_parent", None)
    checkpoint.pop("pending_at", None)


class PastPostsCheckpointRefused(MediaTerminalError):
    reason = "unsupported_past_posts_sync_checkpoint"

    def __init__(self):
        super().__init__(self.reason)


@dataclass(frozen=True)
class _Start:
    capture: admission.SourceReadCapture
    stored: dict | None
    envelope: dict
    config: dict
    origin_id: str
    adapter: Any


_ENVELOPE_TYPE = "instagram_past_posts_sync"
_ENVELOPE_KEYS = frozenset(
    {
        "v",
        "type",
        "generation_id",
        "revision",
        "next_lane",
        "next_metadata",
        "recent",
        "history",
        "pending_helpers",
        "deferred_helpers",
        "progress",
    }
)


def _require_envelope(stored: dict) -> None:
    """Refuse the stored dict unless it is a typed envelope of exactly the shape
    :func:`_land` writes: its keys, version, revision, lanes, helper queues and
    progress record.

    The one validator for both readers of a saved checkpoint. The sync's
    :func:`_envelope` runs it before it resumes, and :func:`status` before it
    reads a lane, a queue or the progress record for display. What the sync
    would refuse is therefore read as unreadable, a named
    :class:`PastPostsCheckpointRefused` on either path, never a ``TypeError`` or
    an ``AttributeError`` from indexing into a value of the wrong type.
    """
    if (
        stored.get("type") != _ENVELOPE_TYPE
        or set(stored) - _ENVELOPE_KEYS - {"summary", "scan_seen"}
        or not _ENVELOPE_KEYS <= stored.keys()
        or type(stored["v"]) is not int
        or stored["v"] != 1
        or type(stored["revision"]) is not int
        or stored["revision"] < 0
        or stored["next_lane"] not in ("recent", "history", "helper")
        or stored["next_metadata"] not in ("recent", "history")
        or not isinstance(stored["progress"], dict)
    ):
        raise PastPostsCheckpointRefused()
    if "scan_seen" in stored and (
        type(stored["scan_seen"]) is not int
        or not 0 <= stored["scan_seen"] <= LISTING_CEILING
    ):
        raise PastPostsCheckpointRefused()
    for lane in ("recent", "history"):
        value = stored[lane]
        if (
            not isinstance(value, dict)
            or set(value) != {"checkpoint", "complete"}
            or not isinstance(value["checkpoint"], dict)
            or type(value["complete"]) is not bool
        ):
            raise PastPostsCheckpointRefused()
    for queue in ("pending_helpers", "deferred_helpers"):
        if (
            not isinstance(stored[queue], list)
            or any(
                not isinstance(x, str) or not x.isdecimal() or not 1 <= len(x) <= 64
                for x in stored[queue]
            )
            or len(set(stored[queue])) != len(stored[queue])
        ):
            raise PastPostsCheckpointRefused()


def _envelope(stored, capture):
    if stored is None:
        return dict(
            v=1,
            type=_ENVELOPE_TYPE,
            generation_id=capture.generation_id,
            revision=0,
            scan_seen=0,
            next_lane="recent",
            next_metadata="recent",
            recent=_fresh_lane(),
            history=_fresh_lane(),
            pending_helpers=[],
            deferred_helpers=[],
            progress={},
        )
    if not isinstance(stored, dict):
        raise PastPostsCheckpointRefused()
    if stored.get("type") != _ENVELOPE_TYPE:
        # Legacy provider state is interpreted by the adapter before
        # dispatch. Only its known version can enter the new lane envelope.
        if type(stored.get("v")) is not int or stored["v"] not in (1, 2):
            raise PastPostsCheckpointRefused()
        result = _envelope(None, capture)
        # Infer imported progress after the sole adapter normalizes legacy state.
        result.pop("scan_seen")
        result["history"]["checkpoint"] = copy.deepcopy(stored)
        return result
    _require_envelope(stored)
    if stored["generation_id"] != capture.generation_id:
        return _envelope(None, capture)
    return copy.deepcopy(stored)


async def _guard(conn, capture, job, ports):
    await admission.check_source_read(conn, capture, capability=ports.capability)
    await jobs.assert_lease(conn, job["id"], job["lease_token"])


async def _stored_checkpoint(conn, capture):
    row = await readers.row(
        conn,
        "SELECT sync_checkpoint FROM media_sources WHERE workspace_id=:ws AND id=:sid",
        ws=capture.workspace_id,
        sid=capture.source_id,
    )
    return row["sync_checkpoint"]


async def _start(engine, job, ports):
    payload = job.get("payload") or {}
    capture = admission.capture_from_payload(payload.get("source_read_capture"))
    if capture.workspace_id != str(job["workspace_id"]) or capture.source_id != str(
        payload.get("source_id")
    ):
        raise admission.SourceReadRefused("source_capture_mismatch", capture.source_id)
    async with unit_of_work(
        engine, capture.workspace_id, actor_kind="worker"
    ).begin() as conn:
        await admission.check_source_read(conn, capture, capability=ports.capability)
        row = await readers.row(
            conn,
            "SELECT config,sync_checkpoint,origin_ig_account_id,clock_timestamp() AS db_now FROM media_sources WHERE workspace_id=:ws AND id=:sid",
            ws=capture.workspace_id,
            sid=capture.source_id,
        )
        envelope = _envelope(row["sync_checkpoint"], capture)
        restart = (
            job.get("kind") == "sync_media_source"
            and envelope["recent"]["complete"]
            and envelope["history"]["complete"]
        )
        if restart:
            envelope["recent"] = _fresh_lane()
            envelope["scan_seen"] = 0
            envelope["next_lane"] = "recent"
            for parent in envelope["deferred_helpers"]:
                if parent not in envelope["pending_helpers"]:
                    envelope["pending_helpers"].append(parent)
            envelope["deferred_helpers"] = []
        await jobs.assert_lease(conn, job["id"], job["lease_token"])
    provider = admission.admission_provider(
        engine, capability=ports.capability, job=job
    )
    adapter = await ports.prepare_adapter(capture, provider)
    if (
        restart
        or row["sync_checkpoint"] is None
        or row["sync_checkpoint"].get("generation_id", capture.generation_id)
        != capture.generation_id
    ):
        upper = int(row["db_now"].timestamp()) + 1
        recent = dict(
            v=2,
            upper=upper,
            until=upper,
            window_start=max(0, upper - WINDOW_SECONDS),
            boundary=[],
            seen=0,
            more=True,
            ceiling_reached=False,
        )
        lower = recent["window_start"]
        envelope["recent"]["checkpoint"] = recent
        if lower > LAUNCH_FLOOR and not restart:
            envelope["history"]["checkpoint"] = {
                **recent,
                "until": lower,
                "window_start": max(LAUNCH_FLOOR, lower - WINDOW_SECONDS),
            }
        else:
            envelope["history"]["complete"] = True
    # Validate unused lanes too. Unsupported state must never ride forward
    # inside a committed envelope while another lane makes a valid read.
    for lane in ("recent", "history"):
        envelope[lane]["checkpoint"] = adapter.normalize_checkpoint(
            envelope[lane]["checkpoint"]
        )
    # Legacy split lanes count one coordinated traversal, never the lifetime
    # catalog. Existing frozen bounds and boundaries remain untouched.
    if "scan_seen" not in envelope:
        envelope["scan_seen"] = min(
            LISTING_CEILING,
            sum(
                envelope[metadata_lane]["checkpoint"].get("seen", 0)
                for metadata_lane in ("recent", "history")
            ),
        )
    active_seen = max(
        envelope["recent"]["checkpoint"].get("seen", 0),
        envelope["history"]["checkpoint"].get("seen", 0)
        if not envelope["history"]["complete"]
        else 0,
    )
    if envelope["scan_seen"] < active_seen:
        raise PastPostsCheckpointRefused()
    lane = envelope["next_lane"]
    if lane in ("recent", "history"):
        checkpoint = envelope[lane]["checkpoint"]
        checkpoint["seen"] = envelope["scan_seen"]
        checkpoint["ceiling_reached"] = envelope["scan_seen"] == LISTING_CEILING
        if checkpoint["ceiling_reached"]:
            _clamp_ceiling(checkpoint)
        envelope[lane]["checkpoint"] = adapter.normalize_checkpoint(checkpoint)
    return _Start(
        capture,
        row["sync_checkpoint"],
        envelope,
        row["config"],
        str(row["origin_ig_account_id"]),
        adapter,
    )


async def _fault(fault, stage):
    if fault is not None:
        await fault(stage)


def _require_interval(expected_progress_interval_seconds):
    if (
        isinstance(expected_progress_interval_seconds, bool)
        or not isinstance(expected_progress_interval_seconds, (int, float))
        or not math.isfinite(expected_progress_interval_seconds)
        or expected_progress_interval_seconds <= 0
    ):
        raise ValueError("positive expected progress interval required")


async def status(
    conn,
    *,
    context,
    source_id,
    capability,
    expected_progress_interval_seconds=BASELINE_SECONDS,
    _checkpoint=None,
):
    """One past-posts source's coverage and scan facts, read under *context*.

    The source half of :func:`progress`, with no destination's verdicts: its
    authority, health, current-generation coverage and checkpoint. A caller
    already holding the trusted context reads several sources under it (the
    mix resource's readiness projection). Like :func:`progress`, displaying a
    source never grants permission for provider I/O.

    Raises :class:`PastPostsCheckpointRefused`, after its statements have run, when
    the saved checkpoint of the counted generation is not readable: a malformed
    envelope (:func:`_require_envelope`) or a progress time that does not parse.
    What an unreadable past-posts source shows is the caller's to decide.
    """
    _require_interval(expected_progress_interval_seconds)
    source_policy.require_context(conn, context, context.workspace_id)
    ws, sid = context.workspace_id, str(source_id)
    authority = await readers.row(
        conn,
        admission.AUTHORITY_SQL,
        ws=ws,
        sid=sid,
        activated=is_activated(capability),
    )
    if authority is None:
        raise admission.SourceReadRefused("source_not_found", sid)
    generation = str(authority["generation_id"]) if authority["generation_id"] else None
    health = await readers.row(
        conn,
        "SELECT s.state,COALESCE((s.config->>'removed')::boolean,false) AS removed,s.created_at,s.source_use_epoch,s.sync_checkpoint,COALESCE(x.ratio>0,false) AS enabled,a.state AS origin_state,a.provider_account_ref,g.lifecycle,c.state AS credential_state,(c.expires_at IS NULL OR c.expires_at>clock_timestamp()) AS unexpired,EXISTS(SELECT 1 FROM media_source_destinations d WHERE d.workspace_id=s.workspace_id AND d.source_id=s.id AND d.ig_account_id=s.origin_ig_account_id AND d.state='active') AS assigned FROM media_sources s LEFT JOIN category_post_case_mix x ON x.workspace_id=s.workspace_id AND x.source_id=s.id AND x.effective_to IS NULL LEFT JOIN ig_accounts a ON a.workspace_id=s.workspace_id AND a.id=s.origin_ig_account_id LEFT JOIN instagram_past_posts_generations g ON g.workspace_id=s.workspace_id AND g.source_id=s.id AND g.id=s.current_authorization_generation_id LEFT JOIN oauth_credentials c ON c.workspace_id=g.workspace_id AND c.ig_account_id=g.origin_ig_account_id AND c.id=g.credential_id WHERE s.workspace_id=:ws AND s.id=:sid",
        ws=ws,
        sid=sid,
    )
    facts = await readers.row(
        conn,
        "SELECT count(*) FILTER (WHERE i.data_generation_id=CAST(:gid AS uuid)) AS observed,"
        "count(*) FILTER (WHERE i.data_generation_id=CAST(:gid AS uuid) AND EXISTS "
        "(SELECT 1 FROM instagram_media_assets a WHERE a.workspace_id=i.workspace_id "
        "AND a.source_id=i.source_id AND a.parent_media_id=i.parent_media_id "
        "AND a.data_generation_id=i.data_generation_id AND a.media_item_id IS NOT NULL)) AS represented,"
        "count(*) FILTER (WHERE i.data_generation_id IS DISTINCT FROM CAST(:gid AS uuid)) AS awaiting_reobservation,"
        "min(i.original_at) FILTER (WHERE i.data_generation_id=CAST(:gid AS uuid)) AS coverage_from,"
        "max(i.original_at) FILTER (WHERE i.data_generation_id=CAST(:gid AS uuid)) AS coverage_to,"
        "(SELECT count(*) FROM instagram_media_assets a WHERE a.workspace_id=:ws AND a.source_id=:sid "
        "AND a.data_generation_id=CAST(:gid AS uuid) AND a.media_item_id IS NOT NULL) AS items,"
        "(SELECT count(*) FROM instagram_media_assets a JOIN instagram_media_index p "
        "ON p.workspace_id=a.workspace_id AND p.source_id=a.source_id "
        "AND p.parent_media_id=a.parent_media_id WHERE a.workspace_id=:ws AND a.source_id=:sid "
        "AND a.data_generation_id=CAST(:gid AS uuid) AND p.slide_count IS NOT NULL) AS slides,"
        "count(*) FILTER (WHERE i.data_generation_id=CAST(:gid AS uuid) "
        "AND i.slide_count IS NOT NULL AND NOT i.slides_complete) AS carousel_children_truncated "
        "FROM instagram_media_index i WHERE i.workspace_id=:ws AND i.source_id=:sid",
        ws=ws,
        sid=sid,
        gid=generation,
    )
    cp = (_checkpoint if _checkpoint is not None else health["sync_checkpoint"]) or {}
    cp = cp if isinstance(cp, dict) and cp.get("generation_id") == generation else {}
    if cp:
        # The checkpoint of the counted generation is read below by indexing
        # into its lanes, queues and progress: it is the sync's own envelope or
        # a named refusal, which the mix projection degrades.
        _require_envelope(cp)
    helpers = len(cp.get("pending_helpers", [])) + len(cp.get("deferred_helpers", []))
    remaining = helpers > 0 or not all(
        cp.get(lane, {}).get("complete", False) for lane in ("recent", "history")
    )
    retired = health["removed"]
    connected = bool(
        not retired
        and generation
        and health["lifecycle"] == "active"
        and health["credential_state"] == "active"
        and health["unexpired"]
        and health["origin_state"] == "active"
        and not (health["provider_account_ref"] or "manual:").startswith("manual:")
        and health["assigned"]
    )
    enabled = bool(health["enabled"] and not retired)
    indexing = bool(enabled and authority["admitted"] and remaining)
    audit = await readers.row(
        conn,
        "SELECT max(created_at) AS at FROM audit_events WHERE workspace_id=:ws AND entity_kind='instagram_source' AND entity_id=:sid AND detail->>'source_use_epoch'=CAST(:epoch AS text)",
        ws=ws,
        sid=sid,
        epoch=str(health["source_use_epoch"]),
    )
    anchors = [audit["at"] or health["created_at"]]
    for value in cp.get("progress", {}).values():
        if isinstance(value, dict) and value.get("last_committed_at"):
            try:
                at = datetime.fromisoformat(value["last_committed_at"])
                if at.utcoffset() is not None:
                    anchors.append(at)
            except (TypeError, ValueError):
                raise PastPostsCheckpointRefused() from None
    return dict(
        generation_id=generation,
        observed=int(facts["observed"]),
        represented=int(facts["represented"]),
        items=int(facts["items"]),
        slides=int(facts["slides"]),
        carousel_children_truncated=int(facts["carousel_children_truncated"]),
        awaiting_reobservation=int(facts["awaiting_reobservation"]),
        coverage_from=facts["coverage_from"].isoformat()
        if facts["coverage_from"]
        else None,
        coverage_to=facts["coverage_to"].isoformat() if facts["coverage_to"] else None,
        pending_helpers=helpers,
        connected=connected,
        enabled=enabled,
        usable=bool(authority["admitted"]),
        indexing=indexing,
        partial=bool(
            remaining
            or any(
                cp.get(lane, {}).get("checkpoint", {}).get("ceiling_reached", False)
                for lane in ("recent", "history")
            )
        ),
        needs_reconnect=bool(
            not retired and (not connected or health["state"] == "error")
        ),
        stalled=bool(
            indexing
            and (context.evaluated_at - max(anchors)).total_seconds()
            >= 2 * expected_progress_interval_seconds
        ),
    )


async def progress(
    conn,
    *,
    capture,
    destination_id,
    capability,
    existing_intent_id=None,
    expected_progress_interval_seconds=BASELINE_SECONDS,
    _checkpoint=None,
):
    """Fresh DB display facts; admission and eligibility remain their shared SQL.

    An old capture locates the source, not the counted generation. Displaying
    disabled or disconnected sources never grants permission for provider I/O.
    Eligible/ready mean verified byte readiness, not metadata candidate readiness.
    """
    _require_interval(expected_progress_interval_seconds)
    context = await source_policy.suitability_context(
        conn,
        workspace_id=capture.workspace_id,
        ig_account_ids=[destination_id],
        capability=capability,
    )
    source = await status(
        conn,
        context=context,
        source_id=capture.source_id,
        capability=capability,
        expected_progress_interval_seconds=expected_progress_interval_seconds,
        _checkpoint=_checkpoint,
    )
    generation = source["generation_id"]
    verdicts = await readers.rows(
        conn,
        "SELECT 'candidate' AS verdict_mode,v.* FROM ("
        + source_policy.CANDIDATE_SQL
        + ") v UNION ALL SELECT 'strict' AS verdict_mode,v.* FROM ("
        + source_policy.SUITABILITY_SQL
        + ") v",
        **context.binds(),
        acct=str(destination_id),
        sid=capture.source_id,
        media=None,
        intent=existing_intent_id,
    )
    strict = [
        v
        for v in verdicts
        if v["verdict_mode"] == "strict"
        and (str(v["data_generation_id"]) if v["data_generation_id"] else None)
        == generation
    ]
    current = [
        v
        for v in verdicts
        if v["verdict_mode"] == "candidate"
        and (str(v["data_generation_id"]) if v["data_generation_id"] else None)
        == generation
    ]
    policy = await readers.row(
        conn,
        "SELECT revision FROM instagram_source_policies WHERE workspace_id=:ws AND source_id=:sid",
        ws=capture.workspace_id,
        sid=capture.source_id,
    )
    reasons = {}
    for verdict in current:
        if verdict["reason"]:
            reasons[verdict["reason"]] = reasons.get(verdict["reason"], 0) + 1
    result = dict(
        generation_id=generation,
        policy_revision=int(policy["revision"]),
        as_of=context.evaluated_at.isoformat(),
        observed=source["observed"],
        represented=source["represented"],
        items=source["items"],
        slides=source["slides"],
        carousel_children_truncated=source["carousel_children_truncated"],
        candidates=sum(v["suitable"] for v in current),
        eligible=sum(v["suitable"] for v in strict),
        verified_ready=sum(v["suitable"] for v in strict),
        awaiting_reobservation=source["awaiting_reobservation"],
        coverage_from=source["coverage_from"],
        coverage_to=source["coverage_to"],
        candidate_exclusions=reasons,
        exclusions={
            reason: sum(v["reason"] == reason for v in strict)
            for reason in {v["reason"] for v in strict if v["reason"]}
        },
    )
    result.update(
        indexed=result["observed"],
        pending_helpers=source["pending_helpers"],
        connected=source["connected"],
        enabled=source["enabled"],
        usable=source["usable"],
        indexing=source["indexing"],
        partial=source["partial"],
        ready=result["candidates"] > 0,
        needs_reconnect=source["needs_reconnect"],
        stalled=source["stalled"],
    )
    return result


def _next(envelope, *, after):
    metadata = envelope["next_metadata"]
    if envelope[metadata]["complete"]:
        other = "history" if metadata == "recent" else "recent"
        metadata = other if not envelope[other]["complete"] else None
    if after in ("recent", "history") and envelope["pending_helpers"]:
        return "helper"
    if metadata:
        return metadata
    if envelope["pending_helpers"]:
        return "helper"
    return None


def _helper_batch(envelope):
    """How many queued parents the next helper turn selects, at most.

    Once the source has a candidate, as many as a turn's request budget reads.
    Until the stored summary reports one, the turn is sized by the requests the
    helper lane has already made: one parent, then a doubling batch (1, 1, 2,
    4, ... up to the budget). The turn that reaches the first candidate then
    commits after the few requests that find it, not after a whole budget, and
    a source that never has one pays about five more turns and reads no more
    parents. A checkpoint with no summary yet says nothing and keeps the budget.
    """
    summary = envelope.get("summary")
    if not isinstance(summary, dict) or summary.get("candidates") != 0:
        return MAX_PAGE_REQUESTS
    asked = envelope["progress"].get("helper", {}).get("graph_attempts", 0)
    return min(MAX_PAGE_REQUESTS, max(1, asked))


async def _helper_candidates(engine, job, start, ports):
    capture = start.capture
    queue = start.envelope["pending_helpers"]
    async with unit_of_work(
        engine, capture.workspace_id, actor_kind="worker"
    ).begin() as conn:
        await admission.check_source_read(conn, capture, capability=ports.capability)
        context = await source_policy.suitability_context(
            conn,
            workspace_id=capture.workspace_id,
            ig_account_ids=[start.origin_id],
            capability=ports.capability,
        )
        # Age only orders helper work. Eligibility is still the shared query. A
        # turn can afford at most one request per parent, so it needs no more
        # candidates than its request budget (`_helper_batch`: fewer until the
        # source has a candidate); the adapter reads what fits.
        rows = await readers.rows(
            conn,
            "SELECT i.parent_media_id,i.data_generation_id,i.observation_revision "
            "FROM instagram_media_index i JOIN instagram_source_policies p "
            "ON p.workspace_id=i.workspace_id AND p.source_id=i.source_id "
            "WHERE i.workspace_id=:ws AND i.source_id=:sid AND i.data_generation_id=:gid "
            "AND i.parent_media_id=ANY(CAST(:ids AS text[])) "
            "AND (:drain OR i.original_at <= CAST(:at AS timestamptz)"
            "-GREATEST(p.minimum_age_days,CAST(:ttl AS integer)+1)*interval '1 day') "
            "ORDER BY (EXISTS (SELECT 1 FROM instagram_media_assets a WHERE "
            "a.workspace_id=i.workspace_id AND a.source_id=i.source_id "
            "AND a.parent_media_id=i.parent_media_id AND a.media_kind='image') "
            "AND i.original_at <= CAST(:at AS timestamptz)"
            "-GREATEST(p.minimum_age_days,CAST(:ttl AS integer)+1)*interval '1 day') "
            "DESC,i.original_at DESC,i.parent_media_id LIMIT :lim",
            ws=capture.workspace_id,
            sid=capture.source_id,
            gid=capture.generation_id,
            ids=queue,
            drain=start.envelope["history"]["complete"],
            at=context.evaluated_at,
            ttl=context.resolved_repost_ttl_days,
            lim=_helper_batch(start.envelope),
        )
        await jobs.assert_lease(conn, job["id"], job["lease_token"])
    return [
        _ParentCapture(
            r["parent_media_id"],
            str(r["data_generation_id"]),
            r["observation_revision"],
        )
        for r in rows
    ]


@dataclass(frozen=True)
class _ParentCapture:
    parent_media_id: str
    generation_id: str
    observation_revision: int


async def _capture_helper_terminal(conn, capture, candidate):
    """A read of the post Instagram refused for good retires all its known
    slide items, atomically. The number of items it removed, or None when the
    index moved meanwhile."""
    params = dict(
        ws=capture.workspace_id,
        sid=capture.source_id,
        parent=candidate.parent_media_id,
        gid=candidate.generation_id,
        rev=candidate.observation_revision,
    )
    moved = await conn.execute(
        text(
            "UPDATE instagram_media_index SET availability='unavailable',"
            "exclusion_reason='asset_unavailable' WHERE workspace_id=:ws AND source_id=:sid "
            "AND parent_media_id=:parent AND data_generation_id=CAST(:gid AS uuid) "
            "AND observation_revision=:rev"
        ),
        params,
    )
    if moved.rowcount != 1:
        return None
    removed = await conn.execute(
        text(
            "UPDATE media_items m SET state='removed' FROM instagram_media_assets a "
            "WHERE a.workspace_id=:ws AND a.source_id=:sid AND a.parent_media_id=:parent "
            "AND a.data_generation_id=CAST(:gid AS uuid) AND a.observation_revision=:rev "
            "AND m.workspace_id=a.workspace_id AND m.source_id=a.source_id "
            "AND (m.id=a.media_item_id OR "
            "m.content_hash='instagram_past_posts:media:'||a.asset_media_id)"
        ),
        params,
    )
    await conn.execute(
        text(
            "UPDATE instagram_media_assets SET availability='unavailable',"
            "exclusion_reason='asset_unavailable',validation_state='pending',"
            "validation_generation_id=NULL,validation_observation_revision=NULL,"
            "validation_fingerprint=NULL,validated_at=NULL,validation_capture=NULL "
            "WHERE workspace_id=:ws AND source_id=:sid AND parent_media_id=:parent "
            "AND data_generation_id=CAST(:gid AS uuid) AND observation_revision=:rev"
        ),
        params,
    )
    return removed.rowcount


async def _land(engine, job, start, ports, *, page, failed=(), fault=None):
    capture = start.capture
    lane = start.envelope["next_lane"]
    envelope = copy.deepcopy(start.envelope)
    graph = page.counters.graph_attempts
    async with unit_of_work(
        engine, capture.workspace_id, actor_kind="worker"
    ).begin() as conn:
        await admission.check_source_read(conn, capture, capability=ports.capability)
        if await _stored_checkpoint(conn, capture) != start.stored:
            return _attempt_result(
                "dropped", lane, job=job, page=page, drop_reason="checkpoint_changed"
            )
        retired_posts, told = [], []
        for candidate in failed:
            removed = await _capture_helper_terminal(conn, capture, candidate)
            if removed is None:
                await conn.rollback()
                return _attempt_result(
                    "dropped",
                    lane,
                    job=job,
                    page=page,
                    drop_reason="helper_capture_changed",
                )
            retired_posts.append((candidate.parent_media_id, removed))
        # A parent the turn could not read waits for the next restart; one it did
        # not reach stays queued.
        for parent in page.failed:
            if parent in envelope["pending_helpers"]:
                envelope["pending_helpers"].remove(parent)
            if parent in envelope["deferred_helpers"]:
                envelope["deferred_helpers"].remove(parent)
        for parent in page.deferred:
            if parent in envelope["pending_helpers"]:
                envelope["pending_helpers"].remove(parent)
            if parent not in envelope["deferred_helpers"]:
                envelope["deferred_helpers"].append(parent)
        committed = 0
        for observation in sorted(page.observations, key=lambda o: o.parent_media_id):
            landed = await instagram_catalog.write_observation(
                conn,
                workspace_id=capture.workspace_id,
                source_id=capture.source_id,
                generation_id=capture.generation_id,
                captured_epoch=capture.source_use_epoch,
                observation=observation,
                keys=ports.tombstone_keys,
                capability=ports.capability,
            )
            if not landed.accepted:
                raise admission.SourceReadRefused(
                    "source_capture_mismatch", capture.source_id
                )
            committed += 1
            if landed.kept or landed.unlisted or landed.retired:
                told.append((observation, landed))
            parent = observation.parent_media_id
            if parent in envelope["deferred_helpers"]:
                envelope["deferred_helpers"].remove(parent)
            if lane in ("recent", "history"):
                if parent not in envelope["pending_helpers"]:
                    envelope["pending_helpers"].append(parent)
            elif parent in envelope["pending_helpers"]:
                envelope["pending_helpers"].remove(parent)
            await _fault(fault, "observation")
        now = (await readers.row(conn, "SELECT clock_timestamp() AS at"))["at"]
        if lane in ("recent", "history"):
            envelope[lane] = {
                "checkpoint": dict(page.checkpoint),
                "complete": not page.checkpoint.get("more", False)
                or lane == "recent"
                and page.checkpoint.get("window_start")
                != start.envelope["recent"]["checkpoint"].get("window_start"),
            }
            envelope["next_metadata"] = "history" if lane == "recent" else "recent"
            envelope["scan_seen"] = page.checkpoint.get("seen", envelope["scan_seen"])
            if envelope["scan_seen"] == LISTING_CEILING:
                for metadata_lane in ("recent", "history"):
                    envelope[metadata_lane]["complete"] = True
                    _clamp_ceiling(envelope[metadata_lane]["checkpoint"])
        previous = envelope["progress"].get(lane, {})
        # Pending enrichment is attempted work without committed parent progress.
        envelope["progress"][lane] = dict(
            previous,
            observations=previous.get("observations", 0) + committed,
            graph_attempts=previous.get("graph_attempts", 0) + graph,
            retry_count=previous.get("retry_count", 0) + _retry_count(job),
        )
        if committed or lane in ("recent", "history"):
            envelope["progress"][lane]["last_committed_at"] = now.isoformat()
        envelope["summary"] = await progress(
            conn,
            capture=capture,
            destination_id=start.origin_id,
            capability=ports.capability,
            _checkpoint=envelope,
        )
        await _fault(fault, "progress")
        envelope["revision"] += 1
        envelope["summary"]["checkpoint_revision"] = envelope["revision"]
        next_lane = _next(envelope, after=lane)
        envelope["next_lane"] = next_lane or "recent"
        await _guard(conn, capture, job, ports)
        moved = await conn.execute(
            text(
                "UPDATE media_sources SET sync_checkpoint=CAST(:new AS jsonb),next_sync_at=CASE WHEN :done THEN clock_timestamp()+make_interval(secs=>:baseline) ELSE next_sync_at END WHERE workspace_id=:ws AND id=:sid AND sync_checkpoint IS NOT DISTINCT FROM CAST(:old AS jsonb)"
            ),
            dict(
                ws=capture.workspace_id,
                sid=capture.source_id,
                new=json.dumps(envelope),
                old=json.dumps(start.stored) if start.stored is not None else None,
                done=next_lane is None,
                baseline=BASELINE_SECONDS,
            ),
        )
        if moved.rowcount != 1:
            raise RuntimeError("past-posts checkpoint compare-and-swap failed")
        await _fault(fault, "checkpoint")
        if next_lane:
            await jobs.enqueue(
                conn,
                kind="first_ingest_chunk",
                workspace_id=capture.workspace_id,
                serialization_key="src:" + capture.source_id,
                payload={
                    "v": 3,
                    "source_id": capture.source_id,
                    "checkpoint_revision": envelope["revision"],
                    "source_read_capture": capture.payload(),
                },
            )
        await _fault(fault, "continuation")
        await _fault(fault, "before_commit")
        await _guard(conn, capture, job, ports)
    # Told once the turn has committed, so no line names a change it rolled back.
    for parent, removed in retired_posts:
        logger.warning(
            "instagram sync: post %s retired: Instagram refused a read of it for"
            " good; %d slide item(s) removed",
            parent,
            removed,
        )
    for observation, landed in told:
        logger.warning(
            "instagram sync: post %s, a complete read of %d slide(s): kept %s for"
            " the stories that hold them, held %s out of the draw, retired %s",
            observation.parent_media_id,
            len(observation.assets),
            list(landed.kept),
            list(landed.unlisted),
            list(landed.retired),
        )
    return _attempt_result("committed", lane, job=job, page=page, committed=committed)


async def _persistent_error(engine, job, start, ports, exc):
    from src.services.target import prompts, outbox

    capture = start.capture
    async with unit_of_work(
        engine, capture.workspace_id, actor_kind="worker"
    ).begin() as conn:
        await admission.check_source_read(conn, capture, capability=ports.capability)
        if await _stored_checkpoint(conn, capture) != start.stored:
            return _attempt_result(
                "dropped",
                start.envelope["next_lane"],
                job=job,
                error=exc,
                drop_reason="checkpoint_changed",
            )
        await jobs.assert_lease(conn, job["id"], job["lease_token"])
        changed = await conn.execute(
            text(
                "UPDATE media_sources SET state='error',alerted_at=clock_timestamp() WHERE workspace_id=:ws AND id=:sid AND alerted_at IS NULL RETURNING id"
            ),
            dict(ws=capture.workspace_id, sid=capture.source_id),
        )
        if changed.first() is not None:
            await outbox.fanout_notification(
                conn,
                workspace_id=capture.workspace_id,
                bindings=await prompts.push_bindings(conn, capture.workspace_id),
                text="⚠️ This workspace's media connection needs to be renewed. Reconnect the source to resume syncing."
                if isinstance(exc, MediaCredentialDead)
                else "⚠️ This workspace's media source is unavailable. Restore access or reconnect the source to resume syncing.",
            )
    return _attempt_result(
        "source-error",
        start.envelope["next_lane"],
        job=job,
        error=exc,
    )


async def run_sync(
    engine, job, *, ports: PastPostsSyncPorts, fault=None
) -> SyncTurnResult:
    """Index one captured metadata turn. Never fetch or decode media bytes."""
    start = page = failure = None
    try:
        start = await _start(engine, job, ports)
        lane = start.envelope["next_lane"]
        failed = ()
        try:
            if lane == "helper":
                selected = await _helper_candidates(engine, job, start, ports)
                if not selected:
                    page = MediaPage([], {}, outcome="parent_enrichment_pending")
                else:
                    page = await start.adapter.enrich_parents(
                        [c.parent_media_id for c in selected],
                        source_id=start.capture.source_id,
                        workspace_id=start.capture.workspace_id,
                    )
                    failed = [c for c in selected if c.parent_media_id in page.failed]
            else:
                page = await start.adapter.list_changes(
                    start.config,
                    start.envelope[lane]["checkpoint"],
                    source_id=start.capture.source_id,
                    workspace_id=start.capture.workspace_id,
                )
        except (MediaCredentialDead, MediaSourceGone) as exc:
            failure = exc
            return await _persistent_error(engine, job, start, ports, exc)
        except MEDIA_FAILURES as exc:
            raise redacted_error(exc) from None
        if not isinstance(page, MediaPage):
            raise MediaTerminalError("past-posts observation page required")
        return await _land(
            engine,
            job,
            start,
            ports,
            page=page,
            failed=failed,
            fault=fault,
        )
    except (admission.SourceReadRefused, MediaSourceFenceDrop) as exc:
        return _attempt_result(
            "dropped",
            start.envelope["next_lane"] if start else None,
            job=job,
            page=page,
            error=failure or exc,
            drop_reason=exc.reason,
        )
    except jobs.JobFenced as exc:
        exc.sync_attempts = _attempt_result(
            "dropped",
            start.envelope["next_lane"] if start else None,
            job=job,
            page=page,
            error=failure or exc,
            drop_reason="lease_fenced",
        )
        raise
