"""Versioned persisted past-posts policy and trusted suitability query context.

No provider calls or production past-posts composition. Policy changes are resources,
not posting commands. Suitability is a database relation; this module supplies its
internal time/TTL/activation context under the canonical transaction locks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


from sqlalchemy import text

from src.exceptions import StorydumpError
from src.services.target import audit, intent_ledger, readers, source_state
from src.services.target.instagram_sources import PastPostsCapability, is_activated

# Reader compatibility, distinct from the schema's future-extensible positive versions.
SUPPORTED_MIX_VERSIONS = (1, 1)  # (policy version, conversion version)

# Shared persisted conversion authority for the reader and the writer.
MIX_CONVERTED_SQL = (
    "source_mix_converted_at IS NOT NULL AND source_mix_revision IS NOT NULL "
    "AND source_mix_mode IN ('equal','custom') "
    "AND source_membership_policy='reset_equal'"
)


def mix_supported(row):
    return (
        row["converted"] is True
        and (row["source_mix_policy_version"], row["source_mix_conversion_version"])
        == SUPPORTED_MIX_VERSIONS
    )


LEGACY_EDITABLE = ("minimum_age_days", "include_credited", "include_unknown")
EDITABLE = (*LEGACY_EDITABLE, "carousel_slides")
CAROUSEL_SLIDES = ("all", "first")
REQUEST_FIELDS = frozenset(("schema_version", "expected_revision", *EDITABLE))
REASONS = (
    "policy_conflict",
    "unsupported_source_policy",
    "invalid_source_policy",
    "source_not_found",
    "unsupported_source_provider",
    "past_posts_not_activated",
)


class PolicyRefused(StorydumpError):
    def __init__(self, reason: str, *, current_revision: int | None = None):
        self.reason = reason
        self.current_revision = current_revision
        super().__init__(reason)


#: What :func:`policy_defaults` materializes into an `instagram_source_policies`
#: row: its columns. The rest of the bundle is a default with no row.
ROW_DEFAULTS = (
    "schema_version",
    "revision",
    "minimum_age_days",
    "include_credited",
    "include_unknown",
    "carousel_slides",
    "eligibility",
    "ranking",
    "destination_scope",
)


def policy_defaults() -> dict:
    """The backend's one versioned definition of creation defaults, v2: a
    fresh bundle each call.

    The :data:`ROW_DEFAULTS` are materialized once, when a source is first
    created, and are the source's own after: a later default changes nothing
    saved. `new_connection_use_history` has no row. It is where the "Use my past
    posts as a content source" switch starts for a NEW connection, read at each
    connect preview (`connect_choice.preview`); the choice a start records, a
    reconnect's saved setting and an existing grant's Off all stand whatever it
    says."""
    return dict(
        schema_version=2,
        revision=1,
        minimum_age_days=90,
        include_credited=False,
        include_unknown=False,
        carousel_slides="all",
        eligibility="supported",
        ranking="none",
        destination_scope="origin_only",
        new_connection_use_history=True,
    )


def validate_update(body: dict) -> dict:
    if not isinstance(body, dict):
        raise PolicyRefused("invalid_source_policy")
    if "schema_version" not in body:
        raise PolicyRefused("invalid_source_policy")
    version = body["schema_version"]
    if type(version) is not int or version not in (1, 2):
        raise PolicyRefused("unsupported_source_policy")
    required = REQUEST_FIELDS if version == 2 else REQUEST_FIELDS - {"carousel_slides"}
    if body.keys() - required:
        raise PolicyRefused("unsupported_source_policy")
    if required - body.keys():
        raise PolicyRefused("invalid_source_policy")
    if version == 2 and body["carousel_slides"] not in CAROUSEL_SLIDES:
        raise PolicyRefused("unsupported_source_policy")
    if (
        type(body["expected_revision"]) is not int
        or body["expected_revision"] < 1
        or type(body["minimum_age_days"]) is not int
        or not 0 <= body["minimum_age_days"] <= 36500
        or any(
            type(body[k]) is not bool for k in ("include_credited", "include_unknown")
        )
    ):
        raise PolicyRefused("invalid_source_policy")
    return dict(body)


def project_policy(saved: dict, raw_repost_ttl_days: int | None) -> dict:
    if (
        type(saved["schema_version"]) is not int
        or saved["schema_version"] not in (1, 2)
        or saved["carousel_slides"] not in CAROUSEL_SLIDES
        or saved["eligibility"] != "supported"
        or saved["ranking"] != "none"
        or saved["destination_scope"] != "origin_only"
    ):
        raise PolicyRefused("unsupported_source_policy")
    return {
        "schema_version": 2,
        "revision": saved["revision"],
        "policy": {
            k: saved[k]
            for k in (*EDITABLE, "eligibility", "ranking", "destination_scope")
        },
        "effective": {
            "minimum_age_days": max(
                saved["minimum_age_days"],
                intent_ledger.resolve_repost_ttl_days(raw_repost_ttl_days) + 1,
            )
        },
        "bounds": {"minimum_age_days": {"min": 0, "max": 36500}},
        "capabilities": {"editable": list(EDITABLE)},
        "choices": {"carousel_slides": list(CAROUSEL_SLIDES)},
    }


async def materialize_policy(executor, *, workspace_id: str, source_id: str) -> None:
    """First creation only; callers already hold the common source locks."""
    source_state.require_source_lock(executor, workspace_id, source_id)
    defaults = policy_defaults()
    await executor.execute(
        text(
            "INSERT INTO instagram_source_policies (workspace_id,source_id,schema_version,revision,"
            "minimum_age_days,include_credited,include_unknown,carousel_slides,eligibility,ranking,destination_scope) "
            "VALUES (:ws,:sid,:schema_version,:revision,:minimum_age_days,:include_credited,"
            ":include_unknown,:carousel_slides,:eligibility,:ranking,:destination_scope)"
        ),
        dict(ws=workspace_id, sid=source_id, **{k: defaults[k] for k in ROW_DEFAULTS}),
    )


async def _lock_policy(executor, ws, sid, capability):
    await source_state.lock_workspace_sources(executor, workspace_id=ws)
    source = await readers.row(
        executor,
        "SELECT provider, origin_ig_account_id FROM media_sources WHERE workspace_id=:ws AND id=:sid",
        ws=ws,
        sid=sid,
    )
    if source is None:
        raise PolicyRefused("source_not_found")
    if source["provider"] != "instagram_past_posts":
        raise PolicyRefused("unsupported_source_provider")
    if not is_activated(capability):
        raise PolicyRefused("past_posts_not_activated")
    await source_state.lock_workspace_sources(
        executor,
        workspace_id=ws,
        ig_account_ids=[source["origin_ig_account_id"]],
        source_ids=[sid],
    )
    saved = await readers.row(
        executor,
        "SELECT p.*,w.repost_ttl_days FROM instagram_source_policies p "
        "JOIN workspaces w ON w.id=p.workspace_id WHERE p.workspace_id=:ws AND p.source_id=:sid FOR UPDATE OF p",
        ws=ws,
        sid=sid,
    )
    if saved is None:
        raise PolicyRefused("unsupported_source_policy")
    project_policy(saved, saved["repost_ttl_days"])
    return saved


async def get_policy(
    executor,
    *,
    workspace_id: str,
    source_id: str,
    capability: PastPostsCapability | None = None,
) -> dict:
    ws, sid = str(workspace_id), str(source_id)
    saved = await _lock_policy(executor, ws, sid, capability)
    return project_policy(saved, saved["repost_ttl_days"])


async def put_policy(
    executor,
    *,
    workspace_id: str,
    source_id: str,
    body: dict,
    capability: PastPostsCapability | None = None,
) -> dict:
    values = validate_update(body)
    ws, sid = str(workspace_id), str(source_id)
    async with source_state.source_savepoint(executor):
        saved = await _lock_policy(executor, ws, sid, capability)
        if saved["revision"] != values["expected_revision"]:
            raise PolicyRefused("policy_conflict", current_revision=saved["revision"])
        values.setdefault("carousel_slides", saved["carousel_slides"])
        updated = await readers.row(
            executor,
            "UPDATE instagram_source_policies SET minimum_age_days=:minimum_age_days,"
            "include_credited=:include_credited,include_unknown=:include_unknown,carousel_slides=:carousel_slides,"
            "schema_version=2,revision=revision+1 "
            "WHERE workspace_id=:ws AND source_id=:sid RETURNING *",
            ws=ws,
            sid=sid,
            **{k: values[k] for k in EDITABLE},
        )
        await audit.record(
            executor,
            workspace_id=ws,
            entity_kind="instagram_source_policy",
            entity_id_sql="CAST(:sid AS uuid)",
            from_state=None,
            to_state=None,
            detail={
                "previous_revision": saved["revision"],
                "revision": updated["revision"],
                "changed": {
                    k: {"from": saved[k], "to": updated[k]}
                    for k in EDITABLE
                    if saved[k] != updated[k]
                },
            },
            sid=sid,
        )
        return project_policy(updated, saved["repost_ttl_days"])


@dataclass(frozen=True)
class SuitabilityContext:
    """Trusted DB-time/TTL/activation binds, for this still-locked transaction.

    Consumers must enter before later item/intent/job locks, keep the builder's
    workspace/source locks until their query commits, and never reuse this after
    commit or supply client overrides. Direct SQL uses these same binds.
    """

    workspace_id: str
    resolved_repost_ttl_days: int
    evaluated_at: datetime
    past_posts_enabled: bool
    transaction: object = field(default=None, repr=False, compare=False)
    nested_transaction: object = field(default=None, repr=False, compare=False)

    def binds(self) -> dict:
        return dict(
            ws=self.workspace_id,
            ttl=self.resolved_repost_ttl_days,
            at=self.evaluated_at,
            activated=self.past_posts_enabled,
        )


async def suitability_context(
    executor,
    *,
    workspace_id: str,
    ig_account_ids=(),
    capability: PastPostsCapability | None = None,
) -> SuitabilityContext:
    ws = str(workspace_id)
    await source_state.lock_all_workspace_sources(
        executor, workspace_id=ws, ig_account_ids=ig_account_ids
    )
    await executor.execute(
        text(
            "SELECT c.id FROM oauth_credentials c JOIN instagram_past_posts_generations g ON g.workspace_id=c.workspace_id AND g.credential_id=c.id JOIN media_sources s ON s.workspace_id=g.workspace_id AND s.id=g.source_id AND s.current_authorization_generation_id=g.id WHERE s.workspace_id=:ws ORDER BY c.id FOR UPDATE OF c"
        ),
        {"ws": ws},
    )
    await executor.execute(
        text(
            "SELECT g.id FROM instagram_past_posts_generations g JOIN media_sources s ON s.workspace_id=g.workspace_id AND s.id=g.source_id AND s.current_authorization_generation_id=g.id WHERE s.workspace_id=:ws ORDER BY g.id FOR UPDATE OF g"
        ),
        {"ws": ws},
    )
    return await _read_context(executor, ws, capability)


async def unlocked_context(
    executor, *, workspace_id: str, capability: PastPostsCapability | None = None
) -> SuitabilityContext:
    """A context with no lock taken, for DISPLAYING a workspace that has not
    converted: its legacy pool reads no past-posts fact and none of the binds, so
    there is nothing for the prefix to protect, and a page load need not
    row-lock what the worker writes (`versioned_mix.resource` reads its state
    first for the same reason). It refuses a workspace whose past-posts source is live,
    whose facts are only one view under the prefix (:func:`suitability_context`)."""
    context = await _read_context(executor, str(workspace_id), capability)
    if context.past_posts_enabled:
        raise RuntimeError("unlocked_context_for_an_activated_workspace")
    return context


async def _read_context(executor, ws: str, capability) -> SuitabilityContext:
    row = await readers.row(
        executor,
        "SELECT repost_ttl_days,source_mix_policy_version,source_mix_conversion_version,"
        "statement_timestamp() AS evaluated_at,"
        f"({MIX_CONVERTED_SQL}) IS TRUE AS converted "
        "FROM workspaces WHERE id=:ws",
        ws=ws,
    )
    if row is None:
        raise PolicyRefused("source_not_found")
    if capability is None:
        capability = executor.info.get("past_posts_capability")
    activated = is_activated(capability) and mix_supported(row)
    return SuitabilityContext(
        ws,
        intent_ledger.resolve_repost_ttl_days(row["repost_ttl_days"]),
        row["evaluated_at"],
        activated,
        _transaction(executor),
        _sync(executor).get_nested_transaction(),
    )


def _sync(executor):
    sync = getattr(executor, "sync_session", None)
    return sync if sync is not None else executor.sync_connection


def _transaction(executor):
    return _sync(executor).get_transaction()


def require_context(executor, context, workspace_id):
    if not isinstance(context, SuitabilityContext) or context.workspace_id != str(
        workspace_id
    ):
        raise ValueError("matching trusted suitability context required")
    if context.transaction is None or context.transaction is not _transaction(executor):
        raise RuntimeError("suitability_context_expired")
    if not context.transaction.is_active or (
        context.nested_transaction is not None
        and not context.nested_transaction.is_active
    ):
        raise RuntimeError("suitability_context_expired")


SUITABILITY_SQL = (
    "SELECT * FROM fn_source_asset_suitability(CAST(:ws AS uuid),CAST(:acct AS uuid),"
    "CAST(:ttl AS integer),CAST(:at AS timestamptz),CAST(:activated AS boolean),"
    "CAST(:sid AS uuid),CAST(:media AS uuid),CAST(:intent AS uuid))"
)


CANDIDATE_SQL = SUITABILITY_SQL[:-1] + ",false)"


async def suitability(
    executor,
    *,
    workspace_id: str,
    ig_account_id: str,
    source_id: str | None = None,
    media_item_id: str | None = None,
    existing_intent_id: str | None = None,
    capability: PastPostsCapability | None = None,
    require_validation: bool = True,
    context: SuitabilityContext | None = None,
) -> list[dict]:
    if type(require_validation) is not bool:
        raise ValueError("boolean validation mode required")
    if context is None:
        context = await suitability_context(
            executor,
            workspace_id=workspace_id,
            ig_account_ids=[ig_account_id],
            capability=capability,
        )
    require_context(executor, context, workspace_id)
    return await readers.rows(
        executor,
        SUITABILITY_SQL if require_validation else CANDIDATE_SQL,
        **context.binds(),
        acct=str(ig_account_id),
        sid=None if source_id is None else str(source_id),
        media=None if media_item_id is None else str(media_item_id),
        intent=None if existing_intent_id is None else str(existing_intent_id),
    )


async def intent_context(executor, *, workspace_id, intent_id):
    """Enter the canonical prefix before a caller locks its selected intent."""
    return await suitability_context(executor, workspace_id=str(workspace_id))


async def intent_suitability(
    executor, *, workspace_id, intent_id, require_validation=True, context=None
):
    if context is None:
        context = await intent_context(
            executor, workspace_id=workspace_id, intent_id=intent_id
        )
    subject = await readers.row(
        executor,
        "SELECT media_item_id,ig_account_id FROM post_intents WHERE workspace_id=:ws AND id=:id",
        ws=str(workspace_id),
        id=str(intent_id),
    )
    if subject is None:
        return None
    result = await suitability(
        executor,
        workspace_id=str(workspace_id),
        ig_account_id=str(subject["ig_account_id"]),
        media_item_id=str(subject["media_item_id"]),
        existing_intent_id=str(intent_id),
        require_validation=require_validation,
        context=context,
    )
    return result[0] if result else None


class MediaAdmissionPending(StorydumpError):
    """A selected live card waits; it must not be silently superseded.

    `evidence` is the refusal's evidence when a fetch was refused
    (`refusal_evidence`); the park records it beside the reason.
    """

    def __init__(self, reason, *, evidence=None):
        self.reason = reason
        self.evidence = evidence
        super().__init__(reason)
