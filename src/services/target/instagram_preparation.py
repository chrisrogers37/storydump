"""Selected past-posts bytes and identity-CAS evidence; never catalog traversal.

The injected adapter owns per-request admission. Every SQL checkpoint closes
before provider/local decoder work. Returned bytes carry proof but never grant
permission: callers reread the shared strict suitability authority.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable
import json

from sqlalchemy import text

from src.services.target import (
    instagram_catalog,
    media_adapters,
    media_validation,
    past_posts_retry,
    readers,
    refusal_evidence,
    source_policy,
    vocabulary,
)
from src.services.target import source_read_admission as admission
from src.services.target.instagram_sources import PastPostsCapability
from src.services.target.instagram_catalog import TombstoneKeys
from src.services.target.unit_of_work import unit_of_work


@dataclass(frozen=True)
class SelectedPastPostsBytes:
    intent_id: str
    capture: admission.SourceReadCapture
    parent_media_id: str = field(repr=False)
    media_kind: str
    content: bytes = field(repr=False)
    validation: media_validation.ValidationResult
    counters: media_adapters.MediaTurnCounters


@dataclass(frozen=True)
class PastPostsPreparationPorts:
    capability: PastPostsCapability
    prepare_adapter: Callable[[admission.SourceReadCapture, dict], Awaitable[Any]]
    tombstone_keys: TombstoneKeys = field(repr=False)
    #: How a refused fetch that proves nothing is tried again (`WorkerConfig`'s
    #: `past_posts_retry_*`).
    retry: past_posts_retry.Retry = past_posts_retry.ONCE


@dataclass(frozen=True)
class _Selected:
    capture: admission.SourceReadCapture
    subject: dict = field(repr=False)
    provenance: dict = field(repr=False)
    ready: bool
    policy_revision: int


_SUBJECT = """
SELECT i.id AS intent_id,i.state AS intent_state,i.cancel_requested,
 m.id AS media_item_id,m.source_id,m.state AS media_item_state,m.media_kind,s.config,s.origin_ig_account_id,
 x.parent_media_id,x.data_generation_id,x.observation_revision,a.validation_fingerprint AS current_fingerprint,
 pol.revision AS policy_revision,
 a.asset_media_id AS selected_asset_id,x.slide_count,
 a.validation_capture AS current_capture,clock_timestamp() AS db_now
FROM post_intents i JOIN media_items m ON m.workspace_id=i.workspace_id AND m.id=i.media_item_id
JOIN media_sources s ON s.workspace_id=m.workspace_id AND s.id=m.source_id
LEFT JOIN instagram_media_assets a ON a.workspace_id=m.workspace_id AND a.source_id=m.source_id AND a.media_item_id=m.id
LEFT JOIN instagram_media_index x ON x.workspace_id=a.workspace_id AND x.source_id=a.source_id
 AND x.parent_media_id=a.parent_media_id AND x.data_generation_id=a.data_generation_id
 AND x.observation_revision=a.observation_revision
LEFT JOIN instagram_source_policies pol ON pol.workspace_id=s.workspace_id AND pol.source_id=s.id
WHERE i.workspace_id=:ws AND i.id=:id AND s.provider='instagram_past_posts'
FOR UPDATE OF m,i
"""


def _tried(generation_id, subject):
    """The failed tries the slide's capture counted for this observation."""
    return past_posts_retry.from_capture(
        subject["current_capture"],
        (generation_id, subject["observation_revision"], subject["selected_asset_id"]),
    )


def _unsuitable(verdict, subject):
    """The pending for a story its suitability verdict refuses. A slide parked
    by an earlier refusal keeps that refusal on its capture, so whichever caller
    meets the latch, its park says why."""
    reason = verdict["reason"] if verdict else "asset_unavailable"
    capture = subject["current_capture"]
    latched = (
        reason == "asset_unavailable"
        and isinstance(capture, dict)
        and capture.get("reason") == "asset_unavailable"
    )
    return source_policy.MediaAdmissionPending(
        reason, evidence=capture.get("refusal") if latched else None
    )


def _refusal(exc, counters, selected) -> dict:
    """A refused fetch's evidence (`refusal_evidence`), with the turn's counters.

    A slide judged gone also carries the slide count the index recorded, to set
    against the children the read returned. A refusal raised with no evidence
    names its error class, so it is never mistaken for none.
    """
    facts = dict(
        refusal_evidence.of(exc)
        or refusal_evidence.evidence("unrecorded", error=type(exc).__name__)
    )
    facts["counters"] = asdict(counters)
    if facts["kind"] == "slide_gone":
        facts["slide_count"] = selected.subject.get("slide_count")
    return facts


def _local_validation(validation, counters, **decided) -> dict:
    """A validation result as evidence: its state, what *decided* it, the byte
    count, the matrix version and the turn's counters. One shape for a refusal
    and a pass."""
    return refusal_evidence.evidence(
        "local_validation",
        state=validation.state,
        **decided,
        bytes=validation.evidence.get("bytes"),
        matrix_version=validation.matrix_version,
        counters=asdict(counters),
    )


def _validation_refusal(validation, counters) -> dict | None:
    """A result that is not supported, as refusal evidence: the check that
    decided it and the values it measured, with the turn's counters."""
    if validation.state == "supported" or not validation.failed_check:
        return None
    return _local_validation(
        validation,
        counters,
        check=validation.failed_check["check"],
        measured=dict(validation.failed_check["measured"]),
    )


def _validation_pass(validation, counters) -> dict | None:
    """A supported result, as evidence in a refusal's shape: each check it
    passed with what it measured and the bound it held."""
    if validation.state != "supported":
        return None
    return _local_validation(
        validation, counters, checks=[dict(check) for check in validation.passed_checks]
    )


class SelectedPreparation:
    def __init__(self, ports: PastPostsPreparationPorts):
        self.ports = ports

    async def _start(self, engine, workspace_id, intent_id):
        ws, iid = str(workspace_id), str(intent_id)
        async with unit_of_work(engine, ws, actor_kind="worker").begin() as conn:
            context = await source_policy.suitability_context(
                conn, workspace_id=ws, capability=self.ports.capability
            )
            subject = await readers.row(conn, _SUBJECT, ws=ws, id=iid)
            if (
                subject is None
                or subject["intent_state"] in vocabulary.TERMINAL_STATES
                or subject["cancel_requested"]
            ):
                raise source_policy.MediaAdmissionPending("asset_unavailable")
            if past_posts_retry.waiting(
                _tried(str(subject["data_generation_id"]), subject), subject["db_now"]
            ):
                # The slide's next try is still ahead: the story waits, before any
                # source read, Graph request or download.
                raise source_policy.MediaAdmissionPending(
                    "past_posts_revalidation_required"
                )
            capture = await admission.capture_source_read(
                conn,
                workspace_id=ws,
                source_id=str(subject["source_id"]),
                capability=self.ports.capability,
            )
            if subject["selected_asset_id"] is None:
                raise source_policy.MediaAdmissionPending(
                    "asset_unavailable"
                    if subject["media_item_state"] == "removed"
                    else "past_posts_revalidation_required"
                )
            if (
                not subject["parent_media_id"]
                or str(subject["data_generation_id"]) != capture.generation_id
            ):
                raise source_policy.MediaAdmissionPending(
                    "past_posts_revalidation_required"
                )
            provenance = await readers.row(
                conn,
                "SELECT * FROM instagram_intent_provenance WHERE workspace_id=:ws AND intent_id=:id FOR UPDATE",
                ws=ws,
                id=iid,
            )
            if provenance is not None:
                if (
                    str(provenance["media_item_id"]) != str(subject["media_item_id"])
                    or provenance["selected_asset_identity"]
                    != subject["selected_asset_id"]
                ):
                    raise source_policy.MediaAdmissionPending(
                        "past_posts_revalidation_required"
                    )
                if provenance["transit_artifact_ref"] is not None and (
                    str(provenance["capture_generation_id"]) != capture.generation_id
                    or provenance["capture_observation_revision"]
                    != subject["observation_revision"]
                    or provenance["validation_fingerprint"]
                    != subject["current_fingerprint"]
                ):
                    raise source_policy.MediaAdmissionPending(
                        "past_posts_revalidation_required"
                    )
            candidate = await source_policy.intent_suitability(
                conn,
                workspace_id=ws,
                intent_id=iid,
                require_validation=False,
                context=context,
            )
            if candidate is None or not candidate["suitable"]:
                raise _unsuitable(candidate, subject)
            if provenance is None:
                await conn.execute(
                    text("""
INSERT INTO instagram_intent_provenance(workspace_id,intent_id,source_id,media_item_id,
 origin_ig_account_id,selection_generation_id,selection_observation_revision,
 capture_generation_id,capture_observation_revision,selected_asset_identity,
 capture_epoch,last_authorized_policy_revision)
VALUES(:ws,:id,:sid,:item,:origin,:gid,:rev,:gid,:rev,:asset,:epoch,:policy)
"""),
                    dict(
                        ws=ws,
                        id=iid,
                        sid=str(subject["source_id"]),
                        item=str(subject["media_item_id"]),
                        origin=str(subject["origin_ig_account_id"]),
                        gid=capture.generation_id,
                        rev=subject["observation_revision"],
                        asset=subject["selected_asset_id"],
                        epoch=capture.source_use_epoch,
                        policy=candidate["policy_revision"],
                    ),
                )
                provenance = await readers.row(
                    conn,
                    "SELECT * FROM instagram_intent_provenance WHERE workspace_id=:ws AND intent_id=:id",
                    ws=ws,
                    id=iid,
                )
            strict = await source_policy.intent_suitability(
                conn, workspace_id=ws, intent_id=iid, context=context
            )
            return _Selected(
                capture,
                subject,
                provenance,
                bool(strict and strict["suitable"]),
                candidate["policy_revision"],
            )

    async def selected_prepare(self, *, engine, workspace_id, intent_id) -> None:
        selected = await self._start(engine, workspace_id, intent_id)
        if not selected.ready:
            await self._fetch(engine, workspace_id, intent_id, selected)

    async def fetch_selected(
        self, *, engine, workspace_id, intent_id
    ) -> SelectedPastPostsBytes:
        selected = await self._start(engine, workspace_id, intent_id)
        return await self._fetch(engine, workspace_id, intent_id, selected)

    async def _fetch(self, engine, workspace_id, intent_id, selected):
        adapter = await self.ports.prepare_adapter(
            selected.capture, selected.subject["config"]
        )
        counters = media_adapters.MediaTurnCounters()

        def observe(value):
            nonlocal counters
            counters = value

        try:
            content, _, _ = await adapter.fetch_bytes(
                selected.subject["config"],
                selected.subject["parent_media_id"],
                source_id=selected.capture.source_id,
                workspace_id=selected.capture.workspace_id,
                selected_asset_id=selected.subject["selected_asset_id"],
                max_bytes=media_validation.SUPPORTED_MEDIA_MATRIX["byte_caps"][
                    selected.subject["media_kind"]
                ],
                on_counters=observe,
            )
        except media_adapters.MediaSelectedAssetChanged as exc:
            # A truncated children read cannot prove the selected slide left: it
            # is tried again after a wait, and parks once its tries are spent.
            refusal = _refusal(exc, counters, selected)
            if await self._land(
                engine, workspace_id, intent_id, selected, None, refusal=refusal
            ):
                raise source_policy.MediaAdmissionPending(
                    "asset_unavailable", evidence=refusal
                ) from None
            raise source_policy.MediaAdmissionPending(
                "past_posts_revalidation_required"
            ) from None
        except media_adapters.MediaSelectedAssetGone as exc:
            # The retirement deletes the asset row, so the story's park is the
            # only place this refusal's evidence lands.
            refusal = _refusal(exc, counters, selected)
            await self._retire_gone(engine, workspace_id, intent_id, selected)
            raise source_policy.MediaAdmissionPending(
                "asset_unavailable", evidence=refusal
            ) from None
        except (
            media_adapters.MediaCredentialDead,
            media_adapters.MediaSourceGone,
            media_adapters.MediaSourceFenceDrop,
        ):
            raise
        except media_adapters.MediaTerminalError as exc:
            refusal = _refusal(exc, counters, selected)
            await self._failure(
                engine,
                workspace_id,
                intent_id,
                selected,
                "asset_unavailable",
                refusal=refusal,
            )
            raise source_policy.MediaAdmissionPending(
                "asset_unavailable", evidence=refusal
            ) from None
        except (
            media_adapters.MediaRetryableError,
            media_adapters.MediaLostResponse,
        ) as exc:
            await self._failure(
                engine,
                workspace_id,
                intent_id,
                selected,
                "download_pending",
                refusal=_refusal(exc, counters, selected),
            )
            raise
        validation = await media_validation.validate_media(
            media_validation.ValidationInput(
                content,
                selected.subject["media_kind"],
                selected.capture.generation_id,
                selected.subject["observation_revision"],
                selected.subject["selected_asset_id"],
            )
        )
        refusal = _validation_refusal(validation, counters)
        await self._land(
            engine,
            workspace_id,
            intent_id,
            selected,
            validation,
            refusal=refusal,
            passed=_validation_pass(validation, counters),
        )
        if validation.state != "supported":
            raise source_policy.MediaAdmissionPending(
                "unsupported"
                if validation.state == "unsupported"
                else "validation_pending",
                evidence=refusal,
            )
        return SelectedPastPostsBytes(
            str(intent_id),
            selected.capture,
            selected.subject["parent_media_id"],
            selected.subject["media_kind"],
            content,
            validation,
            counters,
        )

    async def _failure(self, engine, ws, iid, selected, reason, *, refusal=None):
        await self._land(
            engine, ws, iid, selected, None, reason=reason, refusal=refusal
        )

    async def _retire_gone(self, engine, ws, iid, selected):
        """Retire one selected asset only under fresh source and item authority.

        The helper owns the tombstone and item transition. Returning from this
        transaction commits it before the caller reports the bound story gone.
        """
        ws, iid = str(ws), str(iid)
        async with unit_of_work(engine, ws, actor_kind="worker").begin() as conn:
            await source_policy.suitability_context(
                conn, workspace_id=ws, capability=self.ports.capability
            )
            await admission.check_source_read(
                conn, selected.capture, capability=self.ports.capability
            )
            subject = await readers.row(conn, _SUBJECT, ws=ws, id=iid)
            before = selected.subject
            if (
                subject is None
                or str(subject["source_id"]) != selected.capture.source_id
                or str(subject["media_item_id"]) != str(before["media_item_id"])
                or subject["parent_media_id"] != before["parent_media_id"]
                or subject["intent_state"] in vocabulary.TERMINAL_STATES
                or subject["cancel_requested"]
                or subject["media_item_state"] != "available"
                or str(subject["data_generation_id"]) != selected.capture.generation_id
                or subject["observation_revision"] != before["observation_revision"]
                or subject["selected_asset_id"] != before["selected_asset_id"]
                or subject["policy_revision"] != selected.policy_revision
            ):
                raise source_policy.MediaAdmissionPending(
                    "past_posts_revalidation_required"
                )
            provenance = await readers.row(
                conn,
                "SELECT * FROM instagram_intent_provenance WHERE workspace_id=:ws AND intent_id=:id FOR UPDATE",
                ws=ws,
                id=iid,
            )
            if (
                provenance is None
                or str(provenance["source_id"]) != selected.capture.source_id
                or str(provenance["media_item_id"]) != str(before["media_item_id"])
                or str(provenance["origin_ig_account_id"])
                != str(before["origin_ig_account_id"])
                or provenance["selected_asset_identity"] != before["selected_asset_id"]
            ):
                raise source_policy.MediaAdmissionPending(
                    "past_posts_revalidation_required"
                )
            retired = await instagram_catalog.retire_selected_asset(
                conn,
                workspace_id=ws,
                source_id=selected.capture.source_id,
                generation_id=selected.capture.generation_id,
                observation_revision=subject["observation_revision"],
                asset_media_id=subject["selected_asset_id"],
                media_item_id=str(subject["media_item_id"]),
                keys=self.ports.tombstone_keys,
            )
            if not retired:
                raise source_policy.MediaAdmissionPending(
                    "past_posts_revalidation_required"
                )

    async def _land(
        self,
        engine,
        ws,
        iid,
        selected,
        validation,
        *,
        reason=None,
        refusal=None,
        passed=None,
    ):
        """Record the fetch's outcome on the slide's capture. A refusal with no
        `reason` is the retry rule's to decide (`past_posts_retry`): True when
        it parks the story, else the capture keeps the attempt and the time of
        the next try."""
        # Reacquire and reread in this SAME transaction; _start cannot return an
        # executor or context that outlives its checkpoint.
        async with unit_of_work(engine, str(ws), actor_kind="worker").begin() as conn:
            context = await source_policy.suitability_context(
                conn, workspace_id=str(ws), capability=self.ports.capability
            )
            await admission.check_source_read(
                conn, selected.capture, capability=self.ports.capability
            )
            subject = await readers.row(conn, _SUBJECT, ws=str(ws), id=str(iid))
            if subject is not None and subject["selected_asset_id"] is None:
                raise source_policy.MediaAdmissionPending(
                    "asset_unavailable"
                    if subject["media_item_state"] == "removed"
                    else "past_posts_revalidation_required"
                )
            if (
                subject is None
                or str(subject["data_generation_id"]) != selected.capture.generation_id
                or subject["observation_revision"]
                != selected.subject["observation_revision"]
                or subject["selected_asset_id"] != selected.subject["selected_asset_id"]
            ):
                raise source_policy.MediaAdmissionPending(
                    "past_posts_revalidation_required"
                )
            if (
                subject["cancel_requested"]
                or subject["intent_state"] in vocabulary.TERMINAL_STATES
            ):
                raise source_policy.MediaAdmissionPending("asset_unavailable")
            provenance = await readers.row(
                conn,
                "SELECT * FROM instagram_intent_provenance WHERE workspace_id=:ws AND intent_id=:id FOR UPDATE",
                ws=str(ws),
                id=str(iid),
            )
            if (
                provenance is None
                or provenance["selected_asset_identity"] != subject["selected_asset_id"]
            ):
                raise source_policy.MediaAdmissionPending(
                    "past_posts_revalidation_required"
                )
            if provenance["transit_artifact_ref"] is not None and (
                validation is None
                or provenance["validation_fingerprint"] != validation.fingerprint
                or str(provenance["capture_generation_id"])
                != selected.capture.generation_id
                or provenance["capture_observation_revision"]
                != subject["observation_revision"]
            ):
                raise source_policy.MediaAdmissionPending(
                    "past_posts_revalidation_required"
                )
            verdict = await source_policy.intent_suitability(
                conn,
                workspace_id=str(ws),
                intent_id=str(iid),
                require_validation=False,
                context=context,
            )
            if not verdict or not verdict["suitable"]:
                raise _unsuitable(verdict, subject)
            state = validation.state if validation else "validation_pending"
            decision = None
            if validation is None and reason is None:
                # Decided under the row locks, on the capture and the clock read
                # now, so two refusals of one story count one after the other.
                decision = past_posts_retry.decide(
                    refusal,
                    _tried(selected.capture.generation_id, subject),
                    subject["db_now"],
                    self.ports.retry,
                )
                reason = "asset_unavailable" if decision.park else "download_pending"
            evidence = dict(
                generation_id=selected.capture.generation_id,
                observation_revision=subject["observation_revision"],
                selected_asset_id=subject["selected_asset_id"],
                state=state,
                reason=validation.reason if validation else reason,
            )
            if decision is not None:
                evidence["attempt"] = decision.attempt
                if not decision.park:
                    evidence["next_attempt_at"] = decision.next_attempt_at.isoformat()
            if refusal is not None:
                evidence["refusal"] = refusal
            if passed is not None:
                evidence["passed"] = passed
            if validation:
                evidence.update(
                    fingerprint=validation.fingerprint,
                    matrix_version=validation.matrix_version,
                    evidence_version=validation.evidence_version,
                    evidence=dict(validation.evidence),
                )
            typed = state if state in ("supported", "unsupported") else "pending"
            fingerprint = validation.fingerprint if typed != "pending" else None
            changed = await conn.execute(
                text("""
UPDATE instagram_media_assets SET validation_state=:state,validation_generation_id=CAST(:vgid AS uuid),
 validation_observation_revision=:vrev,validation_fingerprint=:fingerprint,
 validated_at=CASE WHEN CAST(:fingerprint AS text) IS NULL THEN NULL ELSE clock_timestamp() END,
 validation_capture=CAST(:evidence AS jsonb)
WHERE workspace_id=:ws AND source_id=:sid AND media_item_id=:item
 AND data_generation_id=CAST(:gid AS uuid) AND observation_revision=:rev
 AND asset_media_id=:asset
"""),
                dict(
                    ws=str(ws),
                    sid=selected.capture.source_id,
                    item=str(subject["media_item_id"]),
                    gid=selected.capture.generation_id,
                    rev=subject["observation_revision"],
                    asset=subject["selected_asset_id"],
                    state=typed,
                    vgid=selected.capture.generation_id if typed != "pending" else None,
                    vrev=subject["observation_revision"]
                    if typed != "pending"
                    else None,
                    fingerprint=fingerprint,
                    evidence=json.dumps(evidence),
                ),
            )
            if changed.rowcount != 1:
                raise source_policy.MediaAdmissionPending(
                    "past_posts_revalidation_required"
                )
            if provenance["transit_artifact_ref"] is None:
                await conn.execute(
                    text("""
UPDATE instagram_intent_provenance SET capture_generation_id=:gid,capture_observation_revision=:rev,
 validation_fingerprint=:fingerprint,capture_epoch=:epoch,last_authorized_policy_revision=:policy
WHERE workspace_id=:ws AND intent_id=:id
"""),
                    dict(
                        ws=str(ws),
                        id=str(iid),
                        gid=selected.capture.generation_id,
                        rev=subject["observation_revision"],
                        fingerprint=fingerprint,
                        epoch=selected.capture.source_use_epoch,
                        policy=verdict["policy_revision"],
                    ),
                )
        return decision is not None and decision.park
