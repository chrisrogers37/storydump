"""Precise source-ID allocation in the caller's canonical tenant transaction.

Counts are used only once to translate legacy hybrid intent. Destination facts
are supplied by the shared policy projection, never evaluated here.
"""

from __future__ import annotations
from decimal import Decimal, InvalidOperation, localcontext
from sqlalchemy import text
from src.exceptions import StorydumpError
from src.services.target import audit, destination_readiness, readers, source_state
from src.services.target.instagram_sources import is_activated
from src.services.target.source_policy import (
    SUPPORTED_MIX_VERSIONS,
    MIX_CONVERTED_SQL,
    mix_supported,
)
from src.services.target.mix_conversion import MixConversionEvidence
from src.exceptions.mix import MixInvalid
from src.services.target.source_membership import AllocationResult, retire_past_posts


class MixConflict(StorydumpError):
    reason = "mix_conflict"

    def __init__(self, current_revision):
        self.current_revision = current_revision
        super().__init__(self.reason)


class MixProtocolRequired(StorydumpError):
    reason = "mix_protocol_required"
    required_protocol_version = 2


class MixMembershipChange(StorydumpError):
    reason = "mix_membership_change_requires_action"


class MixConversionUnavailable(StorydumpError):
    reason = "mix_conversion_unavailable"


MARKER = "source_mix_mode,source_mix_policy_version,source_membership_policy,source_mix_revision,source_mix_conversion_version,source_mix_converted_at"


async def state(executor, ws):
    saved = await readers.row(
        executor,
        f"SELECT {MARKER},({MIX_CONVERTED_SQL}) IS TRUE AS converted FROM workspaces WHERE id=:ws",
        ws=ws,
    )
    if saved is None:
        raise MixInvalid("unknown_workspace")
    return saved


def converted(saved):
    return mix_supported(saved)


def pending(saved):
    return all(saved[key] is None for key in MARKER.split(","))


def decimal_rows(entries):
    if not isinstance(entries, list):
        raise MixInvalid("not_a_list")
    result = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("source_id"), str):
            raise MixInvalid("empty_source")
        sid = entry["source_id"]
        if sid in result:
            raise MixInvalid("duplicate_source")
        value = entry.get("ratio")
        if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
            raise MixInvalid("bad_ratio")
        try:
            ratio = Decimal(value)
        except (InvalidOperation, ValueError):
            raise MixInvalid("bad_ratio") from None
        if not ratio.is_finite() or not 0 <= ratio <= 1:
            raise MixInvalid("bad_ratio")
        result[sid] = ratio
    with localcontext() as context:
        context.prec = 50
        total = sum(result.values(), Decimal(0))
        if total and abs(total - 1) > Decimal(".001"):
            raise MixInvalid("sum_not_one")
    return result


def normalize_positive(ratios):
    out = dict(ratios)
    positive = sorted(sid for sid, value in out.items() if value > 0)
    if not positive:
        return out
    with localcontext() as context:
        context.prec = 50
        total = sum((out[sid] for sid in positive), Decimal(0))
        out.update({sid: out[sid] / total for sid in positive})
        largest = min(positive, key=lambda sid: (-out[sid], sid))
        out[largest] += 1 - sum(out.values(), Decimal(0))
        if any(out[sid] <= 0 for sid in positive):
            raise MixInvalid("precision_loss")
    return out


def equal(ratios):
    return normalize_positive(
        {sid: Decimal(int(value > 0)) for sid, value in ratios.items()}
    )


async def sources(executor, ws):
    return await readers.rows(
        executor,
        "SELECT s.id::text AS source_id,s.provider,s.state,s.origin_ig_account_id::text AS origin_account_id,a.handle AS origin_handle,COALESCE(s.config->>'folder_name',s.config->>'folder_ref',a.handle,'source') AS name,x.ratio FROM media_sources s LEFT JOIN ig_accounts a ON a.workspace_id=s.workspace_id AND a.id=s.origin_ig_account_id LEFT JOIN category_post_case_mix x ON x.workspace_id=s.workspace_id AND x.source_id=s.id AND x.effective_to IS NULL WHERE s.workspace_id=:ws AND NOT COALESCE((s.config->>'removed')::boolean,false) ORDER BY s.created_at,s.id",
        ws=ws,
    )


def ratio_string(value):
    return format(value, "f") if value else "0"


#: The retained catalog per source, binding `:ws`: every item not in a deleted
#: data generation and not tombstoned, whatever its provider, state, grant or
#: locks. Conversion weighs a legacy Automatic row by it once, and the mix rows
#: report it as `media_count`.
RETAINED_COUNTS_SQL = (
    "SELECT m.source_id::text AS sid,count(*) AS n FROM media_items m"
    " LEFT JOIN instagram_past_posts_generations g ON g.workspace_id=m.workspace_id AND g.id=m.source_data_generation_id"
    " WHERE m.workspace_id=:ws AND g.lifecycle IS DISTINCT FROM 'deleted'"
    " AND NOT EXISTS(SELECT 1 FROM instagram_item_tombstones t WHERE t.workspace_id=m.workspace_id AND t.media_item_id=m.id)"
    " GROUP BY m.source_id"
)


async def row_counts(executor, ws):
    """Each source's `media_count` (:data:`RETAINED_COUNTS_SQL`) and current
    past-posts coverage (`workspaces.PAST_POSTS_COVERAGE_SQL`), in one read."""
    from src.services.target.workspaces import PAST_POSTS_COVERAGE_SQL

    return {
        r["source_id"]: r
        for r in await readers.rows(
            executor,
            "WITH retained AS (" + RETAINED_COUNTS_SQL + "),"
            " coverage AS (" + PAST_POSTS_COVERAGE_SQL + ")"
            " SELECT s.id::text AS source_id,COALESCE(r.n,0) AS media_count,c.observed"
            " FROM media_sources s LEFT JOIN retained r ON r.sid=s.id::text"
            " LEFT JOIN coverage c ON c.workspace_id=s.workspace_id AND c.source_id=s.id"
            " WHERE s.workspace_id=:ws",
            ws=ws,
        )
    }


async def resource(executor, *, workspace_id, destination_projection=None):
    """The posting mix as the web reads it: protocol 1's card, or protocol 2's
    sources, destinations and guidance.

    The conversion state is read first, with no lock, because every workspace
    still answers protocol 1 and that answer needs none: it is plain SELECTs, so
    a page load does not row-lock what the worker writes (the workspace, its
    accounts and its sources). A converted workspace's rows and its saved state
    are one view only under the canonical prefix, which every writer of either
    holds, so the prefix is taken and the state read again under it before
    anything is projected. A conversion that commits between the first read and
    the card's is answered as the protocol-1 card the workspace was when asked;
    the next read is protocol 2, and `guard_legacy` refuses a legacy save made in
    between."""
    from src.services.target import source_state
    from src.services.target.workspaces import observed_count

    ws = str(workspace_id)
    saved = await state(executor, ws)
    if converted(saved):
        await source_state.lock_all_workspace_sources(executor, workspace_id=ws)
        saved = await state(executor, ws)
    if not converted(saved):
        if not pending(saved):
            raise MixProtocolRequired()
        from src.services.target.category_mix import mix_view

        return {
            "protocol_version": 1,
            "rows": await mix_view(executor, workspace_id=ws),
            "supported_actions": ["legacy_save"],
            "conversion": {"state": "pending"},
        }
    rows = await sources(executor, ws)
    if any(r["ratio"] is None for r in rows):
        raise MixInvalid("source_set")
    targets = normalize_positive({r["source_id"]: r["ratio"] for r in rows})
    counts = await row_counts(executor, ws)
    output = [
        {
            **{k: v for k, v in r.items() if k != "ratio"},
            "enabled": r["ratio"] > 0,
            "ratio": ratio_string(r["ratio"]),
            "target_share": ratio_string(targets[r["source_id"]]),
            "media_count": int(counts[r["source_id"]]["media_count"]),
            # Observations are a past-posts source's alone: a Drive folder has none.
            "indexed": observed_count(counts[r["source_id"]]["observed"])
            if r["provider"] == "instagram_past_posts"
            else None,
        }
        for r in rows
    ]
    if destination_projection is None:
        from src.services.target.category_mix import (
            destination_projection as project_destinations,
        )

        destination_projection = project_destinations
    destinations = (
        await destination_projection(executor, workspace_id=ws, rows=output)
        if destination_projection
        else []
    )
    conversion = await readers.row(
        executor,
        "SELECT detail->>'kind' AS kind FROM audit_events WHERE workspace_id=:ws AND entity_kind='source_mix' AND detail ? 'run_id' ORDER BY created_at DESC,id DESC LIMIT 1",
        ws=ws,
    )
    return {
        "protocol_version": 2,
        "policy_version": saved["source_mix_policy_version"],
        "revision": saved["source_mix_revision"],
        "mode": saved["source_mix_mode"],
        "supported_actions": ["reset_equal", "set_enabled", "save_custom"],
        "conversion": {
            "state": "complete",
            "kind": conversion["kind"] if conversion else "legacy_intent",
        },
        # Only a trusted composition root injects the capability; the mix is
        # converted on this branch, and both are required.
        "past_posts_available": is_activated(executor.info.get("past_posts_capability"))
        and converted(saved),
        "rows": output,
        "destinations": destinations,
        "guidance": destination_readiness.guidance(destinations, output),
        "reset_explanation": None,
    }


async def persist(executor, ws, ratios, labels, *, mode, revision, by_user_id=None):
    positives = {sid for sid, value in ratios.items() if value > 0}
    await executor.execute(
        text(
            "UPDATE category_post_case_mix SET effective_to=now() WHERE workspace_id=:ws AND effective_to IS NULL"
        ),
        {"ws": ws},
    )
    for sid, value in ratios.items():
        await executor.execute(
            text(
                "INSERT INTO category_post_case_mix(workspace_id,source_id,category,ratio,created_by_user_id) VALUES(:ws,:sid,:label,:ratio,:by)"
            ),
            {
                "ws": ws,
                "sid": sid,
                "label": labels.get(sid, "source"),
                "ratio": value,
                "by": by_user_id,
            },
        )
    stored = await readers.rows(
        executor,
        "SELECT source_id::text AS sid,ratio FROM category_post_case_mix WHERE workspace_id=:ws AND effective_to IS NULL",
        ws=ws,
    )
    if {r["sid"]: r["ratio"] for r in stored} != ratios or any(
        r["ratio"] <= 0 for r in stored if r["sid"] in positives
    ):
        raise MixInvalid("precision_loss")
    await executor.execute(
        text(
            "UPDATE workspaces SET source_mix_mode=:mode,source_mix_revision=:revision WHERE id=:ws"
        ),
        {"ws": ws, "mode": mode, "revision": revision},
    )


async def mark(executor, ws, evidence, *, revision, kind="new_equal", inputs=None):
    await executor.execute(
        text(
            "UPDATE workspaces SET source_mix_policy_version=:policy,source_membership_policy='reset_equal',source_mix_conversion_version=:conversion,source_mix_converted_at=statement_timestamp(),source_mix_revision=:revision WHERE id=:ws"
        ),
        {
            "ws": ws,
            "policy": SUPPORTED_MIX_VERSIONS[0],
            "conversion": SUPPORTED_MIX_VERSIONS[1],
            "revision": revision,
        },
    )
    await audit.record(
        executor,
        workspace_id=ws,
        entity_kind="source_mix",
        entity_id_sql="CAST(:id AS uuid)",
        id=ws,
        from_state=None,
        to_state=None,
        detail={
            "run_id": evidence.run_id,
            "run_digest": evidence.digest,
            "conversion_version": SUPPORTED_MIX_VERSIONS[1],
            "kind": kind,
            "inputs": inputs or {},
        },
    )


async def initialize(executor, *, workspace_id, evidence=None, protocol_version=None):
    if protocol_version not in (None, 1, 2) or isinstance(protocol_version, bool):
        raise MixProtocolRequired()
    if not isinstance(evidence, MixConversionEvidence):
        if protocol_version == 2:
            raise MixConversionUnavailable()
        return
    await executor.execute(
        text("UPDATE workspaces SET source_mix_mode='equal' WHERE id=:ws"),
        {"ws": str(workspace_id)},
    )
    await mark(executor, str(workspace_id), evidence, revision=0)


async def convert_workspace(executor, *, workspace_id, evidence=None):
    ws = str(workspace_id)
    async with source_state.source_savepoint(executor):
        await source_state.lock_all_workspace_sources(executor, workspace_id=ws)
        saved = await state(executor, ws)
        if converted(saved):
            return await resource(executor, workspace_id=ws)
        if not pending(saved):
            raise MixProtocolRequired()
        if not isinstance(evidence, MixConversionEvidence):
            raise MixConversionUnavailable()
        rows = await sources(executor, ws)
        ratios = {r["source_id"]: r["ratio"] for r in rows}
        positive = [
            value for value in ratios.values() if value is not None and value > 0
        ]
        automatic = [sid for sid, value in ratios.items() if value is None]
        kind = (
            "hybrid_to_custom"
            if positive and automatic
            else "explicit_preserved"
            if positive
            else "automatic_to_equal"
        )
        inputs = {
            "positive_sources": len(positive),
            "automatic_sources": len(automatic),
            "off_sources": sum(
                value == 0 for value in ratios.values() if value is not None
            ),
        }
        mode = "custom" if positive else "equal"
        if not positive:
            ratios = equal(
                {
                    sid: Decimal(1) if value is None else value
                    for sid, value in ratios.items()
                }
            )
        elif automatic:
            # Retained catalog counts deliberately ignore grants, state and locks.
            counts = await readers.rows(executor, RETAINED_COUNTS_SQL, ws=ws)
            n = {r["sid"]: max(1, r["n"]) for r in counts}
            inputs["retained_counts"] = {
                sid: n.get(sid, 1)
                for sid, value in ratios.items()
                if value is None or value > 0
            }
            with localcontext() as context:
                context.prec = 50
                total = sum(positive, Decimal(0))
                n_auto = sum(n.get(sid, 1) for sid in automatic)
                n_explicit = sum(
                    n.get(sid, 1)
                    for sid, value in ratios.items()
                    if value is not None and value > 0
                )
                minimum = min(positive) / total
                share = min(
                    Decimal(n_auto) / Decimal(n_auto + n_explicit),
                    minimum / (1 + minimum),
                )
                ratios = {
                    sid: share * Decimal(n.get(sid, 1)) / Decimal(n_auto)
                    if value is None
                    else (1 - share) * value / total
                    for sid, value in ratios.items()
                }
            ratios = normalize_positive(ratios)
        labels = {r["source_id"]: r["name"] for r in rows}
        await persist(executor, ws, ratios, labels, mode=mode, revision=1)
        await mark(executor, ws, evidence, revision=1, kind=kind, inputs=inputs)
        return await resource(executor, workspace_id=ws)


def check_revision(saved, expected):
    if type(expected) is not int or expected != saved["source_mix_revision"]:
        raise MixConflict(saved["source_mix_revision"])


async def allocate_membership(executor, request):
    # The membership service owns the prefix and past-posts state transition.
    ws = str(request.workspace_id)
    saved = await state(executor, ws)
    if not converted(saved):
        raise MixProtocolRequired()
    check_revision(saved, request.expected_revision)
    rows = await sources(executor, ws)
    ratios = {r["source_id"]: r["ratio"] for r in rows}
    ratios[request.source_id] = Decimal(1) if request.enabled else Decimal(0)
    if request.reset_to_equal:
        ratios = equal(ratios)
    labels = {r["source_id"]: r["name"] for r in rows}
    mode = "equal" if request.reset_to_equal else saved["source_mix_mode"]
    await persist(
        executor, ws, ratios, labels, mode=mode, revision=request.expected_revision + 1
    )
    return AllocationResult(request.expected_revision + 1, mode)


async def mutate(
    executor,
    *,
    workspace_id,
    body,
    by_user_id,
    capability=None,
    destination_projection=None,
):
    ws = str(workspace_id)
    async with source_state.source_savepoint(executor):
        await source_state.lock_all_workspace_sources(executor, workspace_id=ws)
        saved = await state(executor, ws)
        if not converted(saved) or body.get("protocol_version") != 2:
            raise MixProtocolRequired()
        check_revision(saved, body.get("expected_revision"))
        rows = await sources(executor, ws)
        ratios = {r["source_id"]: r["ratio"] for r in rows}
        labels = {r["source_id"]: r["name"] for r in rows}
        action = body.get("action")
        if action == "set_enabled":
            sid = body.get("source_id")
            selected = next((r for r in rows if r["source_id"] == sid), None)
            if selected and selected["provider"] == "instagram_past_posts":
                from src.services.target.source_membership import set_past_posts_enabled

                await set_past_posts_enabled(
                    executor,
                    workspace_id=ws,
                    source_id=sid,
                    enabled=body.get("enabled"),
                    expected_mix_revision=saved["source_mix_revision"],
                    allocation_writer=allocate_membership,
                    capability=capability,
                )
                result = await resource(
                    executor,
                    workspace_id=ws,
                    destination_projection=destination_projection,
                )
                result["reset_explanation"] = "enabled_membership_changed"
                return result
        ratios, mode, changed = plan(saved, rows, body)
        if not changed:
            return await resource(
                executor, workspace_id=ws, destination_projection=destination_projection
            )
        await persist(
            executor,
            ws,
            ratios,
            labels,
            mode=mode,
            revision=saved["source_mix_revision"] + 1,
            by_user_id=by_user_id,
        )
        result = await resource(
            executor, workspace_id=ws, destination_projection=destination_projection
        )
        if action in ("reset_equal", "set_enabled"):
            result["reset_explanation"] = (
                "enabled_membership_changed"
                if action == "set_enabled"
                else "equal_enabled_sources"
            )
        return result


def plan(saved, rows, body):
    ratios = {r["source_id"]: r["ratio"] for r in rows}
    if any(value is None for value in ratios.values()):
        raise MixInvalid("source_set")
    mode = saved["source_mix_mode"]
    action = body.get("action")
    if action == "save_custom":
        custom = decimal_rows(body.get("rows"))
        if set(custom) != set(ratios):
            raise MixInvalid("source_set")
        if any((custom[sid] > 0) != (ratios[sid] > 0) for sid in ratios):
            raise MixMembershipChange()
        return normalize_positive(custom), "custom", True
    if action == "reset_equal":
        return equal(ratios), "equal", True
    if action == "set_enabled":
        sid = body.get("source_id")
        enabled = body.get("enabled")
        if sid not in ratios or type(enabled) is not bool:
            raise MixInvalid("source_set")
        if (ratios[sid] > 0) == enabled:
            return ratios, mode, False
        ratios[sid] = Decimal(int(enabled))
        return equal(ratios), "equal", True
    raise MixInvalid("action")


class _PreviewRollback(Exception):
    """Successful SQL-only simulation must roll back, including nested helpers."""


async def preview(executor, *, workspace_id, body):
    from copy import deepcopy

    ws = str(workspace_id)
    await source_state.lock_all_workspace_sources(executor, workspace_id=ws)
    try:
        async with source_state.source_savepoint(executor):
            result = deepcopy(
                await mutate(
                    executor,
                    workspace_id=ws,
                    body=body,
                    by_user_id=None,
                    capability=executor.info.get("past_posts_capability"),
                )
            )
            raise _PreviewRollback()
    except _PreviewRollback:
        result["revision"] = body["expected_revision"]
        return result


async def connect_drive(executor, *, workspace_id, body):
    from src.services.target import provisioning, media_sync
    from src.services.target.source_membership import AllocationRequest

    ws = str(workspace_id)
    async with source_state.source_savepoint(executor):
        await source_state.lock_all_workspace_sources(executor, workspace_id=ws)
        saved = await state(executor, ws)
        versioned = converted(saved)
        if versioned:
            if body.get("protocol_version") != 2:
                raise MixProtocolRequired()
            check_revision(saved, body.get("expected_revision"))
        elif not pending(saved) or body.get("protocol_version") == 2:
            raise MixProtocolRequired()
        enabled = body.get("enabled", True)
        if type(enabled) is not bool:
            raise MixInvalid("source_set")
        ref = provisioning.folder_ref_from(body.get("folder_ref"))
        old = await readers.row(
            executor,
            "SELECT s.id::text AS sid,COALESCE((s.config->>'removed')::boolean,false) AS removed,x.ratio FROM media_sources s LEFT JOIN category_post_case_mix x ON x.workspace_id=s.workspace_id AND x.source_id=s.id AND x.effective_to IS NULL WHERE s.workspace_id=:ws AND s.provider='gdrive' AND s.config->>'folder_ref'=:ref",
            ws=ws,
            ref=ref,
        )
        sid, created = await provisioning.get_or_create_media_source(
            executor,
            workspace_id=ws,
            folder_ref=ref,
            root_name=body.get("root_name"),
            folder_name=body.get("folder_name"),
        )
        await media_sync.rearm_after_connect(executor, workspace_id=ws, source_id=sid)
        if versioned and (created or old["removed"]):
            on = enabled
            await allocate_membership(
                executor,
                AllocationRequest(ws, sid, on, True, on, saved["source_mix_revision"]),
            )
        result = {"source_id": sid, "created": created}
        if versioned:
            result["mix"] = await resource(executor, workspace_id=ws)
        return result


async def remove_drive(executor, *, workspace_id, source_id, body):
    from src.services.target import provisioning
    from src.services.target.source_membership import AllocationRequest

    ws, sid = str(workspace_id), str(source_id)
    async with source_state.source_savepoint(executor):
        await source_state.lock_all_workspace_sources(executor, workspace_id=ws)
        saved = await state(executor, ws)
        versioned = converted(saved)
        if versioned:
            if body.get("protocol_version") != 2:
                raise MixProtocolRequired()
            check_revision(saved, body.get("expected_revision"))
        elif not pending(saved) or body.get("protocol_version") == 2:
            raise MixProtocolRequired()
        past_posts = await readers.row(
            executor,
            "SELECT id FROM media_sources WHERE workspace_id=:ws AND id=:sid AND provider='instagram_past_posts'",
            ws=ws,
            sid=sid,
        )
        if past_posts is not None:
            # A past-posts source is retired, not paused: every use stops and its origin
            # assignment retires, through the epoch writer. The origin account,
            # its credential and the source's history stay, and a source already
            # removed is a replay that changes nothing.
            await retire_past_posts(
                executor,
                workspace_id=ws,
                source_id=sid,
                expected_mix_revision=saved["source_mix_revision"],
                allocation_writer=allocate_membership,
            )
            return {
                "source_id": sid,
                "state": "removed",
                "mix": await resource(executor, workspace_id=ws),
            }
        rows = await sources(executor, ws)
        old = next(
            (r for r in rows if r["source_id"] == sid and r["provider"] == "gdrive"),
            None,
        )
        paused = await provisioning.pause_media_source(
            executor, workspace_id=ws, source_id=sid
        )
        if not paused:
            return None
        if versioned and old:
            await allocate_membership(
                executor,
                AllocationRequest(
                    ws, sid, False, True, old["ratio"] > 0, saved["source_mix_revision"]
                ),
            )
        result = {"source_id": sid, "state": "paused"}
        if versioned:
            result["mix"] = await resource(executor, workspace_id=ws)
        return result
