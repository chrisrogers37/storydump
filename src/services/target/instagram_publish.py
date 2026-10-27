"""Past-posts upload ownership and atomic stage/permit provenance.

Selected bytes are input only. Every media authorization comes from the shared
strict database relation under its canonical outer prefix. Completion owns only
creation evidence: a cancelled/stale waiter cannot stage or issue an effect.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import logging
import uuid

from sqlalchemy import text

from src.exceptions import StorydumpError
from src.services.target import (
    audit,
    instagram_artifacts,
    jobs,
    readers,
    refusal_evidence,
    source_policy,
    vocabulary,
)
from src.services.target import source_read_admission as admission
from src.services.target.transit import PastPostsUploadUncertain
from src.services.target.unit_of_work import unit_of_work

logger = logging.getLogger(__name__)


def _park_notice(evidence) -> str:
    """The park's notice: why it was refused, in plain words
    (`refusal_evidence.plain`), or that no reason was recorded. Never a cause
    the record cannot support."""
    reason = refusal_evidence.plain(evidence) or "no reason was recorded"
    return (
        "This story from your Instagram past posts could not be prepared."
        f" Refused: {reason}. Open the Queue to review it."
    )


class PastPostsEffectRefused(StorydumpError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


async def lock_pipeline(
    conn, *, workspace_id, intent_id, capability=None, capture=None, effects=True
):
    """Canonical prefix then item/intent; the caller takes the lease LAST."""
    ws, iid = str(workspace_id), str(intent_id)
    context = await source_policy.suitability_context(
        conn, workspace_id=ws, capability=capability
    )
    if capture is not None:
        await admission.check_source_read(conn, capture, capability=capability)
    subject = await readers.row(
        conn,
        "SELECT i.id,i.state,i.cancel_requested,i.publish_step,i.transit_asset_ref,i.ig_container_id,"
        "i.media_item_id,i.ig_account_id,m.source_id,m.state AS media_item_state,m.media_kind,s.provider,s.source_use_epoch,"
        "s.current_authorization_generation_id,g.lifecycle AS current_lifecycle,x.observation_revision,"
        "a.asset_media_id AS current_asset,a.validation_fingerprint AS current_fingerprint,"
        "pol.revision AS policy_revision "
        "FROM post_intents i JOIN media_items m ON m.workspace_id=i.workspace_id AND m.id=i.media_item_id "
        "JOIN media_sources s ON s.workspace_id=m.workspace_id AND s.id=m.source_id "
        "LEFT JOIN instagram_past_posts_generations g ON g.workspace_id=s.workspace_id AND g.id=s.current_authorization_generation_id "
        "LEFT JOIN instagram_media_assets a ON a.workspace_id=m.workspace_id AND a.source_id=m.source_id AND a.media_item_id=m.id "
        "LEFT JOIN instagram_media_index x ON x.workspace_id=a.workspace_id AND x.source_id=a.source_id AND x.parent_media_id=a.parent_media_id AND x.data_generation_id=a.data_generation_id AND x.observation_revision=a.observation_revision "
        "LEFT JOIN instagram_source_policies pol ON pol.workspace_id=s.workspace_id AND pol.source_id=s.id "
        "WHERE i.workspace_id=:ws AND i.id=:id FOR UPDATE OF m,i",
        ws=ws,
        id=iid,
    )
    if subject is not None and subject["provider"] == "instagram_past_posts":
        subject["provenance"] = await readers.row(
            conn,
            "SELECT * FROM instagram_intent_provenance WHERE workspace_id=:ws AND intent_id=:id FOR UPDATE",
            ws=ws,
            id=iid,
        )
    if effects and subject is not None:
        # Existing effect/checkpoint rows are acquired before the lease. New
        # rows serialize through the same locked workspace/intent authority.
        for table, key in (
            ("instagram_artifacts", "artifact_ref"),
            ("provider_operations", "id"),
            ("channel_outbox", "id"),
        ):
            await conn.execute(
                text(
                    f"SELECT {key} FROM {table} WHERE workspace_id=:ws AND intent_id=:id ORDER BY {key} FOR UPDATE"
                ),
                dict(ws=ws, id=iid),
            )
    # A row wait must not freeze the grant/policy evaluation clock.
    context = replace(
        context, evaluated_at=await conn.scalar(text("SELECT clock_timestamp()"))
    )
    return subject, context


async def authorize(
    conn,
    *,
    workspace_id,
    intent_id,
    capability=None,
    locked=None,
    staged=False,
    require_validation=True,
):
    subject, context = locked or await lock_pipeline(
        conn, workspace_id=workspace_id, intent_id=intent_id, capability=capability
    )
    if (
        subject is None
        or subject["provider"] != "instagram_past_posts"
        or subject["cancel_requested"]
    ):
        raise PastPostsEffectRefused("past_posts_content_unavailable")
    if require_validation and subject["state"] not in ("approved", "publishing"):
        raise PastPostsEffectRefused("past_posts_content_unavailable")
    if staged and subject["state"] != "publishing":
        raise PastPostsEffectRefused("past_posts_content_unavailable")
    p = subject.get("provenance")
    if p is None:
        raise PastPostsEffectRefused("past_posts_revalidation_required")
    if subject["current_asset"] is None:
        raise PastPostsEffectRefused(
            "past_posts_content_unavailable"
            if subject["media_item_state"] == "removed"
            else "past_posts_revalidation_required"
        )
    if p["selected_asset_identity"] != subject["current_asset"]:
        raise PastPostsEffectRefused("past_posts_revalidation_required")
    if p["staged_generation_id"] is not None:
        lifecycle = await readers.row(
            conn,
            "SELECT lifecycle FROM instagram_past_posts_generations WHERE workspace_id=:ws AND id=:id",
            ws=str(workspace_id),
            id=str(p["staged_generation_id"]),
        )
        if lifecycle is None or lifecycle["lifecycle"] != "active":
            raise PastPostsEffectRefused("past_posts_generation_deleted")
    verdict = await source_policy.intent_suitability(
        conn,
        workspace_id=str(workspace_id),
        intent_id=str(intent_id),
        context=context,
        require_validation=require_validation,
    )
    if verdict is None or not verdict["suitable"]:
        reason = verdict["reason"] if verdict else "past_posts_content_unavailable"
        if reason == "selected_asset_changed":
            reason = "past_posts_content_changed"
        elif reason == "asset_unavailable":
            # A slide a refused fetch parked: the story parks at once, saying
            # what refused it.
            raise refusal_evidence.attach(
                PastPostsEffectRefused("past_posts_content_unavailable"),
                refusal_evidence.latched(
                    await _slide_capture(conn, workspace_id, subject)
                ),
            )
        elif reason in ("past_posts_revalidation_required", "validation_pending"):
            reason = "past_posts_revalidation_required"
        elif reason not in (
            "source_off",
            "past_posts_generation_deleted",
            "past_posts_content_unavailable",
            "past_posts_content_changed",
        ):
            reason = "source_policy_ineligible"
        raise PastPostsEffectRefused(reason)
    if staged and (
        p["transit_artifact_ref"] is None
        or str(p["staged_generation_id"])
        != str(subject["current_authorization_generation_id"])
        or p["staged_observation_revision"] != subject["observation_revision"]
    ):
        raise PastPostsEffectRefused("past_posts_revalidation_required")
    if staged:
        artifact = await readers.row(
            conn,
            "SELECT outcome,cleanup_state,created_asset_ref,creation_evidence FROM instagram_artifacts WHERE workspace_id=:ws AND artifact_ref=:ref FOR UPDATE",
            ws=str(workspace_id),
            ref=str(p["transit_artifact_ref"]),
        )
        if (
            artifact is None
            or artifact["outcome"] != "settled"
            or artifact["cleanup_state"] != "pending"
            or artifact["creation_evidence"] is None
            or artifact["created_asset_ref"] != subject["transit_asset_ref"]
        ):
            raise PastPostsEffectRefused("past_posts_revalidation_required")
        subject["_stage_artifact"] = artifact
    if require_validation and (
        p["capture_epoch"] != subject["source_use_epoch"]
        or p["last_authorized_policy_revision"] != subject["policy_revision"]
    ):
        await conn.execute(
            text(
                "UPDATE instagram_intent_provenance SET capture_epoch=:epoch,last_authorized_policy_revision=:policy WHERE workspace_id=:ws AND intent_id=:id"
            ),
            dict(
                ws=str(workspace_id),
                id=str(intent_id),
                epoch=subject["source_use_epoch"],
                policy=subject["policy_revision"],
            ),
        )
        await audit.record(
            conn,
            workspace_id=str(workspace_id),
            entity_kind="post_intent",
            entity_id_sql="CAST(:intent AS uuid)",
            intent=str(intent_id),
            from_state=None,
            to_state=None,
            actor_sql=audit.ACTOR_SYSTEM_CHANNEL,
            detail=dict(
                reason="past_posts_authorization_renewed",
                capture_epoch=subject["source_use_epoch"],
                policy_revision=subject["policy_revision"],
            ),
        )
        p = dict(
            p,
            capture_epoch=subject["source_use_epoch"],
            last_authorized_policy_revision=subject["policy_revision"],
        )
        subject["provenance"] = p
    return subject


async def copy_permit(conn, *, operation_id, workspace_id, intent_id, subject):
    p = subject["provenance"]
    await conn.execute(
        text(
            "INSERT INTO instagram_operation_provenance(workspace_id,operation_id,intent_id,source_id,media_item_id,origin_ig_account_id,data_generation_id,observation_revision,policy_revision,capture_epoch,selected_asset_identity,validation_fingerprint,transit_artifact_ref) VALUES(:ws,:op,:intent,:sid,:item,:origin,:gen,:rev,:policy,:epoch,:asset,:fp,:artifact)"
        ),
        dict(
            ws=str(workspace_id),
            op=str(operation_id),
            intent=str(intent_id),
            sid=str(p["source_id"]),
            item=str(p["media_item_id"]),
            origin=str(p["origin_ig_account_id"]),
            gen=str(p["staged_generation_id"]),
            rev=p["staged_observation_revision"],
            policy=p["last_authorized_policy_revision"],
            epoch=p["capture_epoch"],
            asset=p["selected_asset_identity"],
            fp=p["validation_fingerprint"],
            artifact=str(p["transit_artifact_ref"]),
        ),
    )


async def _write_stage(conn, *, workspace_id, intent_id, subject, artifact):
    await conn.execute(
        text(
            "UPDATE instagram_intent_provenance SET staged_generation_id=capture_generation_id,staged_observation_revision=capture_observation_revision,transit_artifact_ref=:artifact WHERE workspace_id=:ws AND intent_id=:id"
        ),
        dict(
            ws=str(workspace_id),
            id=str(intent_id),
            artifact=str(artifact["artifact_ref"]),
        ),
    )
    moved = await conn.execute(
        text(
            "UPDATE post_intents SET transit_asset_ref=:ref,publish_step='transit_uploaded',ig_container_id=NULL WHERE workspace_id=:ws AND id=:id AND state='publishing' RETURNING id"
        ),
        dict(
            ws=str(workspace_id), id=str(intent_id), ref=artifact["created_asset_ref"]
        ),
    )
    if moved.first() is None:
        raise PastPostsEffectRefused("past_posts_content_unavailable")


async def _slide_capture(conn, workspace_id, subject):
    """The locked subject's slide capture (`validation_capture`), or None when
    the slide has no asset row."""
    return await conn.scalar(
        text(
            "SELECT validation_capture FROM instagram_media_assets"
            " WHERE workspace_id=:ws AND source_id=:sid AND media_item_id=:item"
        ),
        dict(
            ws=str(workspace_id),
            sid=str(subject["source_id"]),
            item=str(subject["media_item_id"]),
        ),
    )


def _not_admitted(reason, subject) -> dict:
    """Why the locked subject's preparation was not admitted, as a refusal
    (`not_admitted`), for a park no refusal backs: *reason* and the item's
    state, so the card and the notice say the same."""
    return refusal_evidence.evidence(
        "not_admitted", reason=reason, item_state=subject["media_item_state"]
    )


async def _record_park(conn, *, ws, iid, state, admission_reason, evidence) -> bool:
    """Park a waiting past-posts story for review, in the caller's
    transaction, on the reason its preparation was not admitted. The refusal's
    *evidence* rides with the reason: `last_error -> 'error' -> 'refusal'`,
    the audit detail's `refusal` and the notice (`refusal_evidence`). A slide
    judged gone has no asset row left to hold it. False, and nothing logged,
    when the story had already moved on."""
    from src.services.target import publish_pipeline

    reason = (
        "past_posts_content_changed"
        if admission_reason == "selected_asset_changed"
        else "past_posts_content_unavailable"
    )
    await conn.execute(
        text("UPDATE post_intents SET last_error=CAST(:error AS jsonb) WHERE id=:id"),
        dict(
            id=iid,
            error=json.dumps(
                {
                    "v": 1,
                    "error": {
                        "type": "PastPostsEffectRefused",
                        "reason": reason,
                        "message": reason,
                        "refusal": evidence,
                    },
                }
            ),
        ),
    )
    await audit.record(
        conn,
        workspace_id=ws,
        entity_kind="post_intent",
        entity_id_sql="CAST(:intent AS uuid)",
        intent=iid,
        from_state=state,
        to_state=state,
        detail={"reason": reason, "refusal": evidence},
    )
    tz = await conn.scalar(text("SELECT tz FROM workspaces WHERE id=:ws"), dict(ws=ws))
    parked = await publish_pipeline.park_for_review(
        conn,
        workspace_id=ws,
        intent_id=iid,
        from_state=state,
        tz=str(tz or "UTC"),
        notice=_park_notice(evidence),
    )
    if parked:
        logger.info(
            "past-posts story %s parked for review from %s: %s (%s)",
            iid,
            state,
            reason,
            evidence.get("kind"),
        )
    return parked


class PastPostsPipeline:
    def __init__(self, *, capability, fetch_selected, prepare_selected=None):
        self.capability, self.fetch_selected = capability, fetch_selected
        self.prepare_selected = prepare_selected
        self._completions = set()

    def _uow(self, engine, ws, actor_kind="system"):
        return unit_of_work(engine, str(ws), actor_kind=actor_kind)

    async def selected_prepare(self, *, engine, workspace_id, intent_id, attempt=1):
        """Prepare a bound card, or review definite content loss under fresh authority.

        A refusal alone is not authority: terminal, cancelled, missing and fenced
        subjects cannot acquire a new review transition. Selection stays immutable.
        *attempt* is which try of the file this is (the prepare job's attempts).
        Returns True when the story was parked for review.
        """
        if self.prepare_selected is None:
            raise PastPostsEffectRefused("past_posts_pipeline_not_configured")
        ws, iid = str(workspace_id), str(intent_id)
        # Fence the whole delegated turn, including typed failures. A later
        # current capture cannot turn an old content refusal into new evidence.
        snapshot = None
        async with self._uow(engine, ws).begin() as conn:
            sid = await conn.scalar(
                text(
                    "SELECT m.source_id FROM post_intents i JOIN media_items m ON m.workspace_id=i.workspace_id AND m.id=i.media_item_id WHERE i.workspace_id=:ws AND i.id=:id"
                ),
                dict(ws=ws, id=iid),
            )
            if sid is not None:
                await source_policy.suitability_context(
                    conn, workspace_id=ws, capability=self.capability
                )
                capture = await admission.capture_source_read(
                    conn,
                    workspace_id=ws,
                    source_id=str(sid),
                    capability=self.capability,
                )
                subject, _ = await lock_pipeline(
                    conn,
                    workspace_id=ws,
                    intent_id=iid,
                    capability=self.capability,
                    capture=capture,
                )
                if subject is not None:
                    p = subject.get("provenance")
                    snapshot = (
                        capture,
                        subject["media_item_id"],
                        subject["observation_revision"],
                        subject["current_asset"],
                        p["selected_asset_identity"] if p else subject["current_asset"],
                        subject["policy_revision"],
                    )
        try:
            await self.prepare_selected(
                engine=engine, workspace_id=ws, intent_id=iid, attempt=attempt
            )
            return False
        except source_policy.MediaAdmissionPending as exc:
            if exc.reason not in (
                "selected_asset_changed",
                "asset_unavailable",
                "unsupported",
            ):
                raise
            if snapshot is None:
                raise
            capture, item_id, revision, asset, bound_asset, policy_revision = snapshot
            async with self._uow(engine, ws).begin() as conn:
                subject, _ = await lock_pipeline(
                    conn,
                    workspace_id=ws,
                    intent_id=iid,
                    capability=self.capability,
                    capture=capture,
                )
                retired = (
                    exc.reason == "asset_unavailable"
                    and subject is not None
                    and subject["media_item_state"] == "removed"
                    and subject["observation_revision"] is None
                    and subject["current_asset"] is None
                    and asset is not None
                    and asset == bound_asset
                )
                if subject is not None and (
                    subject["media_item_id"] != item_id
                    or subject["policy_revision"] != policy_revision
                    or (not retired and subject["observation_revision"] != revision)
                    or (not retired and subject["current_asset"] != asset)
                    or (
                        subject.get("provenance") is not None
                        and subject["provenance"]["selected_asset_identity"]
                        != bound_asset
                    )
                ):
                    raise source_policy.MediaAdmissionPending(
                        "past_posts_revalidation_required"
                    ) from None
                if (
                    subject is None
                    or subject["provider"] != "instagram_past_posts"
                    or str(subject["source_id"]) != capture.source_id
                    or subject["state"] not in vocabulary.WAITING_STATES
                    or subject["cancel_requested"]
                    or subject.get("provenance") is None
                ):
                    raise
                evidence = exc.evidence
                if evidence is None and exc.reason == "unsupported":
                    # The check that judged the slide unsupported, read here,
                    # under the story's lock: meeting the latch asked Instagram
                    # nothing, and the prepare job counts a try spent by any
                    # evidence the pending carries.
                    evidence = refusal_evidence.latched(
                        await _slide_capture(conn, ws, subject)
                    )
                await _record_park(
                    conn,
                    ws=ws,
                    iid=iid,
                    state=subject["state"],
                    admission_reason=exc.reason,
                    evidence=evidence or _not_admitted(exc.reason, subject),
                )
            return True

    async def park_late(
        self,
        *,
        engine,
        workspace_id,
        intent_id,
        tries,
        late_seconds,
        actor_kind="system",
    ) -> bool:
        """Park a story whose late window, *late_seconds* after its slot,
        closed before its file could be fetched, as *actor_kind* (the reaper's
        leg passes its own). Its evidence says so (`window_closed`): the *tries*
        its prepare jobs spent, and the refusal its slide last recorded, read
        under the story's lock. That is any refusal the capture holds, a
        waiting one (`download_pending`) included, so not `latched`'s; a
        retired slide, which has no asset row left to hold one, says it is no
        longer indexed (`not_admitted`). No fetch decides it, so no fetch's
        fence is needed: only that the story still waits for its slot. False
        when it no longer does."""
        ws, iid = str(workspace_id), str(intent_id)
        async with self._uow(engine, ws, actor_kind).begin() as conn:
            subject, _ = await lock_pipeline(
                conn, workspace_id=ws, intent_id=iid, capability=self.capability
            )
            if (
                subject is None
                or subject["provider"] != "instagram_past_posts"
                or subject["state"] not in vocabulary.UNSERVED_STATES
                or subject["cancel_requested"]
            ):
                return False
            capture = await _slide_capture(conn, ws, subject)
            last = capture.get("refusal") if isinstance(capture, dict) else None
            if last is None and subject["media_item_state"] == "removed":
                last = _not_admitted("asset_unavailable", subject)
            return await _record_park(
                conn,
                ws=ws,
                iid=iid,
                state=subject["state"],
                admission_reason="asset_unavailable",
                evidence=refusal_evidence.evidence(
                    "window_closed",
                    late_seconds=late_seconds,
                    tries=tries,
                    last_refusal=last,
                ),
            )

    async def _inspect(self, conn, ws, iid, *, capture=None):
        locked = await lock_pipeline(
            conn,
            workspace_id=ws,
            intent_id=iid,
            capability=self.capability,
            capture=capture,
        )
        attempts = await readers.rows(
            conn,
            "SELECT * FROM instagram_artifacts WHERE workspace_id=:ws AND intent_id=:id AND kind='transit' ORDER BY admitted_at DESC FOR UPDATE",
            ws=ws,
            id=iid,
        )
        if any(a["outcome"] in ("unsettled", "uncertain") for a in attempts):
            raise PastPostsEffectRefused("past_posts_upload_uncertain")
        pending = await readers.row(
            conn,
            "SELECT id FROM provider_operations WHERE workspace_id=:ws AND intent_id=:id AND (state='permitted' OR (op_kind='publish' AND state='succeeded')) LIMIT 1",
            ws=ws,
            id=iid,
        )
        if pending is not None:
            raise PastPostsEffectRefused("past_posts_operation_unresolved")
        return locked, attempts

    async def stage(self, *, engine, job, intent_id, transit):
        ws, iid = str(job["workspace_id"]), str(intent_id)
        async with self._uow(engine, ws).begin() as conn:
            locked, attempts = await self._inspect(conn, ws, iid)
            if locked[0] is None or locked[0]["state"] != "publishing":
                raise PastPostsEffectRefused("past_posts_content_unavailable")
            subject = await authorize(
                conn,
                workspace_id=ws,
                intent_id=iid,
                locked=locked,
                require_validation=False,
            )
            p = subject["provenance"]
            for a in attempts:
                if (
                    a["outcome"] == "settled"
                    and a["creation_evidence"] is not None
                    and a["cleanup_state"] == "pending"
                    and str(a["data_generation_id"]) == str(p["capture_generation_id"])
                    and a["observation_revision"] == p["capture_observation_revision"]
                    and a["selected_asset_identity"] == p["selected_asset_identity"]
                    and a["validation_fingerprint"] == p["validation_fingerprint"]
                    and str(a["data_generation_id"])
                    == str(subject["current_authorization_generation_id"])
                    and a["observation_revision"] == subject["observation_revision"]
                    and a["validation_fingerprint"] == subject["current_fingerprint"]
                ):
                    subject = await authorize(
                        conn, workspace_id=ws, intent_id=iid, locked=locked
                    )
                    if subject["transit_asset_ref"] is None:
                        await _write_stage(
                            conn,
                            workspace_id=ws,
                            intent_id=iid,
                            subject=subject,
                            artifact=a,
                        )
                    await jobs.assert_lease(conn, job["id"], job["lease_token"])
                    return a["created_asset_ref"]
            await jobs.assert_lease(conn, job["id"], job["lease_token"])
        selected = await self.fetch_selected(
            engine=engine, workspace_id=ws, intent_id=iid
        )
        async with self._uow(engine, ws).begin() as conn:
            locked, attempts = await self._inspect(
                conn, ws, iid, capture=selected.capture
            )
            subject = await authorize(
                conn, workspace_id=ws, intent_id=iid, locked=locked
            )
            p = subject["provenance"]
            v = selected.validation
            if (
                selected.intent_id != iid
                or v.state != "supported"
                or str(p["capture_generation_id"]) != v.source_generation
                or p["capture_observation_revision"] != v.observation_revision
                or p["selected_asset_identity"] != v.selected_asset_id
                or p["validation_fingerprint"] != v.fingerprint
            ):
                raise PastPostsEffectRefused("past_posts_revalidation_required")
            ref = "ws/" + ws + "/" + uuid.uuid4().hex
            artifact_ref = await instagram_artifacts.admit_upload(
                conn,
                workspace_id=ws,
                source_id=str(p["source_id"]),
                data_generation_id=str(p["capture_generation_id"]),
                observation_revision=p["capture_observation_revision"],
                media_item_id=str(p["media_item_id"]),
                intent_id=iid,
                capture_epoch=p["capture_epoch"],
                policy_revision=p["last_authorized_policy_revision"],
                selected_asset_identity=p["selected_asset_identity"],
                validation_fingerprint=p["validation_fingerprint"],
                admitted_asset_ref=ref,
                admitted_resource_type=selected.media_kind,
                upload_attempt_id=str(uuid.uuid4()),
            )
            await jobs.assert_lease(conn, job["id"], job["lease_token"])
        task = asyncio.create_task(
            self._complete(engine, ws, iid, artifact_ref, ref, selected, transit)
        )
        self._completions.add(task)

        def finished(t):
            self._completions.discard(t)
            if not t.cancelled():
                t.exception()

        task.add_done_callback(finished)
        evidence = await asyncio.shield(task)
        async with self._uow(engine, ws).begin() as conn:
            locked = await lock_pipeline(
                conn,
                workspace_id=ws,
                intent_id=iid,
                capability=self.capability,
                capture=selected.capture,
            )
            subject = await authorize(
                conn, workspace_id=ws, intent_id=iid, locked=locked
            )
            p = subject["provenance"]
            artifact = await readers.row(
                conn,
                "SELECT * FROM instagram_artifacts WHERE workspace_id=:ws AND artifact_ref=:ref FOR UPDATE",
                ws=ws,
                ref=artifact_ref,
            )
            if (
                str(p["capture_generation_id"]) != selected.validation.source_generation
                or p["capture_observation_revision"]
                != selected.validation.observation_revision
                or p["validation_fingerprint"] != selected.validation.fingerprint
                or artifact["created_asset_ref"] != evidence.public_id
            ):
                raise PastPostsEffectRefused("past_posts_revalidation_required")
            await _write_stage(
                conn, workspace_id=ws, intent_id=iid, subject=subject, artifact=artifact
            )
            await jobs.assert_lease(conn, job["id"], job["lease_token"])
        return evidence.public_id

    async def _complete(self, engine, ws, iid, artifact_ref, ref, selected, transit):
        try:
            evidence = await transit.upload(
                selected.content,
                workspace_id=ws,
                media_kind=selected.media_kind,
                preallocated_ref=ref,
            )
        except Exception as exc:
            async with self._uow(engine, ws).begin() as conn:
                await lock_pipeline(
                    conn,
                    workspace_id=ws,
                    intent_id=iid,
                    capability=self.capability,
                    effects=False,
                )
                await instagram_artifacts.mark_uncertain(
                    conn, workspace_id=ws, artifact_ref=artifact_ref
                )
            if isinstance(exc, PastPostsUploadUncertain):
                raise PastPostsEffectRefused("past_posts_upload_uncertain") from None
            raise
        async with self._uow(engine, ws).begin() as conn:
            await lock_pipeline(
                conn,
                workspace_id=ws,
                intent_id=iid,
                capability=self.capability,
                effects=False,
            )
            await instagram_artifacts.record_created(
                conn, workspace_id=ws, artifact_ref=artifact_ref, evidence=evidence
            )
        return evidence

    async def wait_completions(self, *, timeout=95):
        if self._completions:
            await asyncio.wait_for(
                asyncio.shield(
                    asyncio.gather(*tuple(self._completions), return_exceptions=True)
                ),
                timeout=timeout,
            )

    async def release_stale_stage(self, *, engine, workspace_id, intent_id, job=None):
        ws, iid = str(workspace_id), str(intent_id)
        async with self._uow(engine, ws).begin() as conn:
            locked, attempts = await self._inspect(conn, ws, iid)
            subject, _ = locked
            pending = await readers.row(
                conn,
                "SELECT id FROM provider_operations WHERE workspace_id=:ws AND intent_id=:id AND (state='permitted' OR (op_kind='publish' AND state='succeeded')) LIMIT 1",
                ws=ws,
                id=iid,
            )
            if pending is not None:
                raise PastPostsEffectRefused("past_posts_operation_unresolved")
            if subject is None or subject["state"] in (
                "posted",
                "failed",
                "cancelled",
                "expired",
                "skipped",
                "rejected",
                "publishing_ambiguous",
            ):
                raise PastPostsEffectRefused("past_posts_content_unavailable")
            p = subject.get("provenance")
            if p is None or p["transit_artifact_ref"] is None:
                return False
            # The immutable existing artifact already owns pending generation
            # cleanup. Never delete or rewrite its creation/issued-operation proof.
            await conn.execute(
                text(
                    "UPDATE instagram_intent_provenance SET staged_generation_id=NULL,staged_observation_revision=NULL,transit_artifact_ref=NULL WHERE workspace_id=:ws AND intent_id=:id"
                ),
                dict(ws=ws, id=iid),
            )
            await conn.execute(
                text(
                    "UPDATE post_intents SET transit_asset_ref=NULL,ig_container_id=NULL,publish_step='none' WHERE workspace_id=:ws AND id=:id"
                ),
                dict(ws=ws, id=iid),
            )
            if job is not None:
                await jobs.assert_lease(conn, job["id"], job["lease_token"])
            return True

    async def prepare_capture(self, *, engine, job, intent_id, allow_fetch=True):
        ws, iid = str(job["workspace_id"]), str(intent_id)
        try:
            async with self._uow(engine, ws).begin() as conn:
                locked, _ = await self._inspect(conn, ws, iid)
                await authorize(conn, workspace_id=ws, intent_id=iid, locked=locked)
                await jobs.assert_lease(conn, job["id"], job["lease_token"])
                return
        except PastPostsEffectRefused as exc:
            if (
                exc.reason
                not in (
                    "past_posts_revalidation_required",
                    "past_posts_generation_deleted",
                )
                or not allow_fetch
            ):
                raise
        await self.release_stale_stage(
            engine=engine, workspace_id=ws, intent_id=iid, job=job
        )
        await self.fetch_selected(engine=engine, workspace_id=ws, intent_id=iid)
        async with self._uow(engine, ws).begin() as conn:
            locked, _ = await self._inspect(conn, ws, iid)
            await authorize(conn, workspace_id=ws, intent_id=iid, locked=locked)
            await jobs.assert_lease(conn, job["id"], job["lease_token"])

    async def prepare_resume(self, *, engine, job, intent_id, transit):
        ws, iid = str(job["workspace_id"]), str(intent_id)
        needs_release = False
        async with self._uow(engine, ws).begin() as conn:
            locked, _ = await self._inspect(conn, ws, iid)
            subject, _ = locked
            if subject is None:
                raise PastPostsEffectRefused("past_posts_content_unavailable")
            if subject["publish_step"] != "none":
                try:
                    await authorize(
                        conn, workspace_id=ws, intent_id=iid, locked=locked, staged=True
                    )
                    await jobs.assert_lease(conn, job["id"], job["lease_token"])
                    return
                except PastPostsEffectRefused as exc:
                    if exc.reason not in (
                        "past_posts_revalidation_required",
                        "past_posts_generation_deleted",
                    ):
                        raise
                    needs_release = True
            await jobs.assert_lease(conn, job["id"], job["lease_token"])
        if needs_release:
            await self.release_stale_stage(
                engine=engine, workspace_id=ws, intent_id=iid, job=job
            )
        await self.stage(engine=engine, job=job, intent_id=iid, transit=transit)

    async def cleanup_owned(self, *, engine, workspace_id, intent_id, transit):
        ws, iid = str(workspace_id), str(intent_id)
        async with self._uow(engine, ws).begin() as conn:
            await lock_pipeline(
                conn, workspace_id=ws, intent_id=iid, capability=self.capability
            )
            artifacts = await readers.rows(
                conn,
                "SELECT created_asset_ref,admitted_resource_type FROM instagram_artifacts WHERE workspace_id=:ws AND intent_id=:id AND outcome='settled' AND creation_evidence IS NOT NULL AND cleanup_state='pending'",
                ws=ws,
                id=iid,
            )
        for a in artifacts:
            try:
                await transit.destroy(
                    a["created_asset_ref"], media_kind=a["admitted_resource_type"]
                )
            except Exception as exc:
                logger.warning(
                    "past-posts source owned cleanup remains pending (%s)",
                    type(exc).__name__,
                )
        # Legacy bool destroy merges 'ok' and 'not found'. It proves neither
        # pending-transmission drain nor eager derivative completion; do not
        # settle attempts or complete deletion receipts from that answer.
