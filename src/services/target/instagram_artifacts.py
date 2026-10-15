"""Persist already-authorized archive upload admissions and creation evidence.

The caller owns canonical locks, suitability and its tenant transaction before
admission. Completion only records an owned attempt, including after a fence;
it never stages, authorizes or renews. There is no no-effect/drain escape here.
"""

from dataclasses import asdict
import json
import re
import uuid

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from src.exceptions.base import RefusalError
from src.services.target import readers
from src.services.target.transit import ArchiveUploadEvidence


class ArtifactRefused(RefusalError):
    """capture_invalid, upload_already_pending, admitted_reference_conflict,
    attempt_conflict, artifact_missing,
    creation_evidence_invalid or creation_conflict: closed, safe persistence refusal.
    """

    _prefix = "archive artifact refused"


def _uuid(value):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise ArtifactRefused("capture_invalid") from None


def _plain(value):
    return isinstance(value, str) and 0 < len(value) <= 256 and "://" not in value


async def admit_upload(
    executor,
    *,
    workspace_id,
    source_id,
    data_generation_id,
    observation_revision,
    media_item_id,
    intent_id,
    capture_epoch,
    policy_revision,
    selected_asset_identity,
    validation_fingerprint,
    admitted_asset_ref,
    admitted_resource_type,
    upload_attempt_id,
):
    """Persist one authorized possible effect; no provider dispatch is performed."""
    ws = _uuid(workspace_id)
    if (
        type(observation_revision) is not int
        or observation_revision <= 0
        or type(capture_epoch) is not int
        or capture_epoch < 0
        or type(policy_revision) is not int
        or policy_revision <= 0
        or not _plain(selected_asset_identity)
        or not _plain(validation_fingerprint)
        or admitted_resource_type not in ("image", "video")
        or not isinstance(admitted_asset_ref, str)
        or re.fullmatch(re.escape("ws/" + ws) + r"/[0-9a-f]{20,64}", admitted_asset_ref)
        is None
    ):
        raise ArtifactRefused("capture_invalid")
    params = dict(
        ws=ws,
        sid=_uuid(source_id),
        gid=_uuid(data_generation_id),
        rev=observation_revision,
        item=_uuid(media_item_id),
        intent=_uuid(intent_id),
        epoch=capture_epoch,
        policy=policy_revision,
        asset=selected_asset_identity,
        fp=validation_fingerprint,
        ref=admitted_asset_ref,
        resource=admitted_resource_type,
        attempt=_uuid(upload_attempt_id),
    )
    try:
        async with executor.begin_nested():
            result = await executor.execute(
                text(
                    "INSERT INTO instagram_artifacts(workspace_id,source_id,data_generation_id,"
                    "observation_revision,media_item_id,intent_id,kind,capture_epoch,policy_revision,"
                    "selected_asset_identity,validation_fingerprint,upload_attempt_id,admitted_at,outcome,"
                    "admitted_asset_ref,admitted_resource_type) VALUES(CAST(:ws AS uuid),CAST(:sid AS uuid),"
                    "CAST(:gid AS uuid),:rev,CAST(:item AS uuid),CAST(:intent AS uuid),'transit',:epoch,:policy,"
                    ":asset,:fp,CAST(:attempt AS uuid),clock_timestamp(),'unsettled',:ref,:resource) RETURNING artifact_ref"
                ),
                params,
            )
            return str(result.scalar_one())
    except IntegrityError as exc:
        cause = getattr(exc.orig, "__cause__", None)
        constraint = getattr(cause, "constraint_name", None)
        if constraint == "uq_archive_admitted_owner":
            raise ArtifactRefused("admitted_reference_conflict") from None
        if constraint == "uq_archive_unsettled_intent":
            raise ArtifactRefused("upload_already_pending") from None
        if constraint == "uq_archive_upload_attempt":
            raise ArtifactRefused("attempt_conflict") from None
        raise


async def _attempt(executor, workspace_id, artifact_ref):
    found = await readers.row(
        executor,
        "SELECT admitted_asset_ref,admitted_resource_type,outcome,creation_evidence FROM instagram_artifacts "
        "WHERE workspace_id=:ws AND artifact_ref=:ref AND kind='transit' AND admitted_asset_ref IS NOT NULL FOR UPDATE",
        ws=_uuid(workspace_id),
        ref=_uuid(artifact_ref),
    )
    if found is None:
        raise ArtifactRefused("artifact_missing")
    return found


async def mark_uncertain(executor, *, workspace_id, artifact_ref):
    """Retain uncertainty without changing a settled affirmative result."""
    found = await _attempt(executor, workspace_id, artifact_ref)
    if found["outcome"] == "settled":
        return False
    await executor.execute(
        text(
            "UPDATE instagram_artifacts SET outcome='uncertain' WHERE workspace_id=:ws AND artifact_ref=:ref"
        ),
        dict(ws=_uuid(workspace_id), ref=_uuid(artifact_ref)),
    )
    return True


async def mark_destroyed(executor, *, workspace_id, artifact_ref):
    """Record a settled attempt's asset as destroyed, once the caller has
    verified it absent. A compare-and-set on the attempt as the caller read it
    (settled, cleanup pending): False when it is not, so a concurrent run's
    write is never counted twice."""
    moved = await executor.execute(
        text(
            "UPDATE instagram_artifacts SET cleanup_state='destroyed',destroyed_at=clock_timestamp() "
            "WHERE workspace_id=:ws AND artifact_ref=:ref AND kind='transit' AND outcome='settled' "
            "AND cleanup_state='pending'"
        ),
        dict(ws=_uuid(workspace_id), ref=_uuid(artifact_ref)),
    )
    return moved.rowcount > 0


async def record_created(executor, *, workspace_id, artifact_ref, evidence):
    """Matching affirmative creation may land late; cleanup remains pending."""
    if (
        type(evidence) is not ArchiveUploadEvidence
        or not isinstance(evidence.public_id, str)
        or len(evidence.public_id) > 256
        or not isinstance(evidence.asset_id, str)
        or re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", evidence.asset_id) is None
        or type(evidence.version) is not int
        or not 0 < evidence.version <= 2**63 - 1
        or evidence.resource_type not in ("image", "video")
        or evidence.delivery_type != "authenticated"
        or not isinstance(evidence.signature, str)
        or re.fullmatch(r"[0-9a-f]{40}", evidence.signature) is None
    ):
        raise ArtifactRefused("creation_evidence_invalid")
    found = await _attempt(executor, workspace_id, artifact_ref)
    safe = asdict(evidence)
    if (
        evidence.public_id != found["admitted_asset_ref"]
        or evidence.resource_type != found["admitted_resource_type"]
        or (
            found["creation_evidence"] is not None
            and found["creation_evidence"] != safe
        )
    ):
        raise ArtifactRefused("creation_conflict")
    if found["creation_evidence"] is not None:
        return
    await executor.execute(
        text(
            "UPDATE instagram_artifacts SET outcome='settled',settled_at=clock_timestamp(),"
            "created_asset_ref=:created,creation_evidence=CAST(:evidence AS jsonb) WHERE workspace_id=:ws AND artifact_ref=:ref"
        ),
        dict(
            ws=_uuid(workspace_id),
            ref=_uuid(artifact_ref),
            created=evidence.public_id,
            evidence=json.dumps(safe),
        ),
    )
