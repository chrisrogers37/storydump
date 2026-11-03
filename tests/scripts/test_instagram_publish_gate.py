"""Past-posts source durable stage/permit ownership under real tenant roles."""

import asyncio
import importlib.util
import uuid

import pytest
from sqlalchemy import text

from src.services.target import jobs, provider_ops, publish_cap, source_policy
from src.services.target.transit import (
    PastPostsUploadEvidence,
    PastPostsUploadUncertain,
)
from src.services.target.unit_of_work import in_transaction
from tests.scripts import test_instagram_preparation_gate as preparation
from tests.scripts import test_instagram_observations_gate as observations
from tests.scripts import test_instagram_sources_gate as foundation

past_posts_engine = foundation.past_posts_engine
origin = foundation.origin
pytestmark = pytest.mark.integration


def module():
    assert importlib.util.find_spec("src.services.target.instagram_publish"), (
        "durable past-posts coordinator missing"
    )
    from src.services.target import instagram_publish

    return instagram_publish


class Transit:
    def __init__(self, *, hold=None, uncertain=False):
        self.calls = []
        self.hold = hold
        self.started = asyncio.Event()
        self.uncertain = uncertain

    def delivery_url(self, ref, *, media_kind, variant=0):
        return "https://fake-transit.invalid/" + ref

    async def destroy(self, ref, *, media_kind):
        assert not in_transaction()
        return True

    async def upload(self, data, *, workspace_id, media_kind, preallocated_ref):
        assert not in_transaction()
        self.calls.append(preallocated_ref)
        self.started.set()
        if self.hold is not None:
            await self.hold.wait()
        if self.uncertain:
            raise PastPostsUploadUncertain("past-posts upload uncertain")
        return PastPostsUploadEvidence(
            preallocated_ref, "a" * 32, 123, media_kind, "authenticated", "b" * 40
        )


async def ready(engine, chain):
    src, item, intent = await preparation.selected(engine, chain)
    selected, calls = preparation.service(engine, chain)
    await selected.selected_prepare(
        engine=engine, workspace_id=str(chain["ws"]), intent_id=intent
    )
    async with observations.uow(engine, chain) as conn:
        for state in ("prompt_pending", "awaiting_approval"):
            await conn.execute(
                text("UPDATE post_intents SET state=:state WHERE id=:id"),
                dict(state=state, id=intent),
            )
        from src.services.target.unit_of_work import apply_gucs

        await apply_gucs(
            conn,
            tenant_id=str(chain["ws"]),
            actor_kind="user",
            actor_user_id=str(chain["user"]),
        )
        await conn.execute(
            text(
                "UPDATE post_intents SET state='approved',approved_by_user_id=:user WHERE id=:id"
            ),
            dict(user=str(chain["user"]), id=intent),
        )
        await apply_gucs(conn, tenant_id=str(chain["ws"]), actor_kind="system")
        assert (
            await publish_cap.flip_to_publishing(
                conn,
                workspace_id=str(chain["ws"]),
                intent_id=intent,
                ig_account_id=str(chain["iga"]),
                local_date=await conn.scalar(text("SELECT current_date")),
                effective_cap=3,
            )
            is publish_cap.FlipOutcome.PROCEED
        )
        token = str(uuid.uuid4())
        job = dict(
            (
                await conn.execute(
                    text(
                        "INSERT INTO jobs(workspace_id,kind,lane,serialization_key,payload,state,lease_token,locked_by,locked_until,attempts,max_attempts) VALUES(:ws,'publish_pipeline','bulk',:key,CAST(:payload AS jsonb),'leased',:token,'past-posts-gate',clock_timestamp()+interval '5 minutes',1,5) RETURNING *"
                    ),
                    dict(
                        ws=str(chain["ws"]),
                        key="ig:" + str(chain["iga"]),
                        payload='{"v":1,"intent_id":"' + intent + '"}',
                        token=token,
                    ),
                )
            )
            .mappings()
            .one()
        )
    coordinator = module().PastPostsPipeline(
        capability=foundation.capability(), fetch_selected=selected.fetch_selected
    )
    return src, item, intent, job, coordinator, calls


async def state(engine, chain, intent):
    async with observations.uow(engine, chain) as conn:
        row = dict(
            (
                await conn.execute(
                    text(
                        "SELECT i.publish_step,i.transit_asset_ref,p.* FROM post_intents i LEFT JOIN instagram_intent_provenance p ON p.workspace_id=i.workspace_id AND p.intent_id=i.id WHERE i.id=:id"
                    ),
                    dict(id=intent),
                )
            )
            .mappings()
            .one()
        )
        artifacts = [
            dict(x)
            for x in (
                await conn.execute(
                    text("SELECT * FROM instagram_artifacts WHERE intent_id=:id"),
                    dict(id=intent),
                )
            )
            .mappings()
            .all()
        ]
    return row, artifacts


async def off(engine, chain, src):
    async with observations.uow(engine, chain) as conn:
        await conn.execute(
            text(
                "UPDATE category_post_case_mix SET ratio=0 WHERE source_id=:id AND effective_to IS NULL"
            ),
            dict(id=src["source_id"]),
        )


async def test_past_posts_admission_stage_and_permit_copy_are_atomic(
    past_posts_engine, origin
):
    module()
    src, item, intent, job, c, calls = await ready(past_posts_engine, origin)
    transit = Transit()
    ref = await c.stage(
        engine=past_posts_engine, job=job, intent_id=intent, transit=transit
    )
    row, artifacts = await state(past_posts_engine, origin, intent)
    assert row["publish_step"] == "transit_uploaded" and row["transit_asset_ref"] == ref
    assert (
        row["transit_artifact_ref"] == artifacts[0]["artifact_ref"]
        and artifacts[0]["outcome"] == "settled"
    )
    assert artifacts[0]["admitted_asset_ref"] == ref and len(transit.calls) == 1
    async with past_posts_engine.connect() as conn:
        from src.services.target.unit_of_work import apply_gucs

        await apply_gucs(conn, tenant_id=str(origin["ws"]), actor_kind="system")
        permit = await provider_ops.acquire_permit(
            conn,
            workspace_id=str(origin["ws"]),
            intent_id=intent,
            job_id=job["id"],
            lease_token=job["lease_token"],
            op_kind="container_create",
            past_posts_capability=foundation.capability(),
        )
    async with observations.uow(past_posts_engine, origin) as conn:
        copy = (
            (
                await conn.execute(
                    text(
                        "SELECT * FROM instagram_operation_provenance WHERE operation_id=:id"
                    ),
                    dict(id=permit["id"]),
                )
            )
            .mappings()
            .one()
        )
        assert (
            copy["transit_artifact_ref"] == row["transit_artifact_ref"]
            and copy["validation_fingerprint"] == row["validation_fingerprint"]
        )


async def test_uncertain_restart_never_reuploads(past_posts_engine, origin):
    m = module()
    src, item, intent, job, c, calls = await ready(past_posts_engine, origin)
    transit = Transit(uncertain=True)
    with pytest.raises(m.PastPostsEffectRefused, match="past_posts_upload_uncertain"):
        await c.stage(
            engine=past_posts_engine, job=job, intent_id=intent, transit=transit
        )
    restarted = m.PastPostsPipeline(
        capability=foundation.capability(), fetch_selected=c.fetch_selected
    )
    with pytest.raises(m.PastPostsEffectRefused, match="past_posts_upload_uncertain"):
        await restarted.stage(
            engine=past_posts_engine, job=job, intent_id=intent, transit=transit
        )
    row, artifacts = await state(past_posts_engine, origin, intent)
    assert len(transit.calls) == 1 and row["transit_asset_ref"] is None
    assert artifacts[0]["outcome"] == "uncertain" and artifacts[0]["settled_at"] is None


async def test_cancelled_waiter_late_success_after_off_records_cleanup_only(
    past_posts_engine, origin
):
    module()
    src, item, intent, job, c, calls = await ready(past_posts_engine, origin)
    release = asyncio.Event()
    transit = Transit(hold=release)
    waiter = asyncio.create_task(
        c.stage(engine=past_posts_engine, job=job, intent_id=intent, transit=transit)
    )
    await transit.started.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await off(past_posts_engine, origin, src)
    release.set()
    await c.wait_completions()
    row, artifacts = await state(past_posts_engine, origin, intent)
    assert row["publish_step"] == "none" and row["transit_asset_ref"] is None
    assert (
        len(artifacts) == 1
        and artifacts[0]["outcome"] == "settled"
        and artifacts[0]["cleanup_state"] == "pending"
    )
    assert len(transit.calls) == 1


async def test_off_before_admission_denies_upload(past_posts_engine, origin):
    m = module()
    src, item, intent, job, c, calls = await ready(past_posts_engine, origin)
    await off(past_posts_engine, origin, src)
    transit = Transit()
    with pytest.raises(m.PastPostsEffectRefused, match="source_off"):
        await c.stage(
            engine=past_posts_engine, job=job, intent_id=intent, transit=transit
        )
    assert (
        transit.calls == []
        and (await state(past_posts_engine, origin, intent))[1] == []
    )


async def test_stage_failure_rolls_back_bundle_but_keeps_created_owner(
    past_posts_engine, origin, monkeypatch
):
    m = module()
    src, item, intent, job, c, calls = await ready(past_posts_engine, origin)
    original = m._write_stage

    async def fault(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("stage transaction fault")

    monkeypatch.setattr(m, "_write_stage", fault)
    with pytest.raises(RuntimeError, match="stage transaction fault"):
        await c.stage(
            engine=past_posts_engine, job=job, intent_id=intent, transit=Transit()
        )
    row, artifacts = await state(past_posts_engine, origin, intent)
    assert row["publish_step"] == "none" and row["transit_artifact_ref"] is None
    assert (
        artifacts[0]["outcome"] == "settled"
        and artifacts[0]["cleanup_state"] == "pending"
    )


async def test_stale_lease_completion_cannot_stage(past_posts_engine, origin):
    module()
    src, item, intent, job, c, calls = await ready(past_posts_engine, origin)
    release = asyncio.Event()
    transit = Transit(hold=release)
    waiter = asyncio.create_task(
        c.stage(engine=past_posts_engine, job=job, intent_id=intent, transit=transit)
    )
    await transit.started.wait()
    async with observations.uow(past_posts_engine, origin) as conn:
        await conn.execute(
            text(
                "UPDATE jobs SET locked_until=clock_timestamp()-interval '1 second' WHERE id=:id"
            ),
            dict(id=job["id"]),
        )
    release.set()
    with pytest.raises(jobs.JobFenced):
        await waiter
    row, artifacts = await state(past_posts_engine, origin, intent)
    assert (
        row["publish_step"] == "none"
        and row["transit_artifact_ref"] is None
        and artifacts[0]["outcome"] == "settled"
    )


async def permit(engine, chain, intent, job, *, kind="container_create"):
    from src.services.target.unit_of_work import apply_gucs

    async with engine.connect() as conn:
        await apply_gucs(conn, tenant_id=str(chain["ws"]), actor_kind="system")
        return await provider_ops.acquire_permit(
            conn,
            workspace_id=str(chain["ws"]),
            intent_id=intent,
            job_id=job["id"],
            lease_token=job["lease_token"],
            op_kind=kind,
            past_posts_capability=foundation.capability(),
        )


async def test_off_on_renewal_keeps_created_proof_and_does_not_fetch(
    past_posts_engine, origin
):
    module()
    src, item, intent, job, c, calls = await ready(past_posts_engine, origin)
    transit = Transit()
    await c.stage(engine=past_posts_engine, job=job, intent_id=intent, transit=transit)
    before, artifacts = await state(past_posts_engine, origin, intent)
    reads = len(calls)
    await off(past_posts_engine, origin, src)
    async with observations.uow(past_posts_engine, origin) as conn:
        await conn.execute(
            text(
                "UPDATE category_post_case_mix SET ratio=1 WHERE source_id=:id AND effective_to IS NULL"
            ),
            dict(id=src["source_id"]),
        )
        await conn.execute(
            text(
                "UPDATE media_sources SET source_use_epoch=source_use_epoch+2 WHERE id=:id"
            ),
            dict(id=src["source_id"]),
        )
    issued = await permit(past_posts_engine, origin, intent, job)
    after, retained = await state(past_posts_engine, origin, intent)
    assert len(calls) == reads and len(transit.calls) == 1 and retained == artifacts
    assert after["capture_epoch"] == before["capture_epoch"] + 2
    async with observations.uow(past_posts_engine, origin) as conn:
        copied = (
            await conn.execute(
                text(
                    "SELECT capture_epoch FROM instagram_operation_provenance WHERE operation_id=:id"
                ),
                dict(id=issued["id"]),
            )
        ).scalar_one()
        audit = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM audit_events WHERE entity_id=:id AND detail->>'reason'='past_posts_authorization_renewed'"
                ),
                dict(id=intent),
            )
        ).scalar_one()
        assert copied == after["capture_epoch"] and audit == 1


async def test_deleted_stage_never_authorizes_new_permit(past_posts_engine, origin):
    m = module()
    src, item, intent, job, c, calls = await ready(past_posts_engine, origin)
    await c.stage(
        engine=past_posts_engine, job=job, intent_id=intent, transit=Transit()
    )
    row, artifacts = await state(past_posts_engine, origin, intent)
    async with observations.uow(past_posts_engine, origin) as conn:
        await conn.execute(
            text(
                "UPDATE instagram_past_posts_generations SET lifecycle='deletion_pending' WHERE id=:id"
            ),
            dict(id=str(row["staged_generation_id"])),
        )
    with pytest.raises(m.PastPostsEffectRefused, match="past_posts_generation_deleted"):
        await permit(past_posts_engine, origin, intent, job)
    async with observations.uow(past_posts_engine, origin) as conn:
        assert (
            await conn.scalar(
                text("SELECT count(*) FROM provider_operations WHERE intent_id=:id"),
                dict(id=intent),
            )
            == 0
        )
    assert (await state(past_posts_engine, origin, intent))[1] == artifacts


async def test_permit_and_copy_rollback_on_final_lease_failure(
    past_posts_engine, origin, monkeypatch
):
    module()
    src, item, intent, job, c, calls = await ready(past_posts_engine, origin)
    await c.stage(
        engine=past_posts_engine, job=job, intent_id=intent, transit=Transit()
    )

    async def expired(*args, **kwargs):
        return False

    monkeypatch.setattr(provider_ops, "_lease_is_live", expired)
    with pytest.raises(provider_ops.PermitRefused):
        await permit(past_posts_engine, origin, intent, job)
    async with observations.uow(past_posts_engine, origin) as conn:
        assert (
            await conn.scalar(
                text("SELECT count(*) FROM provider_operations WHERE intent_id=:id"),
                dict(id=intent),
            )
            == 0
        )
        assert (
            await conn.scalar(
                text(
                    "SELECT count(*) FROM instagram_operation_provenance WHERE intent_id=:id"
                ),
                dict(id=intent),
            )
            == 0
        )


async def test_uncertain_upload_cannot_release_stage_for_revalidation(
    past_posts_engine, origin
):
    m = module()
    src, item, intent, job, c, calls = await ready(past_posts_engine, origin)
    with pytest.raises(m.PastPostsEffectRefused):
        await c.stage(
            engine=past_posts_engine,
            job=job,
            intent_id=intent,
            transit=Transit(uncertain=True),
        )
    with pytest.raises(m.PastPostsEffectRefused, match="past_posts_upload_uncertain"):
        await c.release_stale_stage(
            engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
        )
    assert len((await state(past_posts_engine, origin, intent))[1]) == 1


async def test_restaging_releases_whole_bundle_and_retains_old_owner(
    past_posts_engine, origin
):
    module()
    src, item, intent, job, c, calls = await ready(past_posts_engine, origin)
    transit = Transit()
    old = await c.stage(
        engine=past_posts_engine, job=job, intent_id=intent, transit=transit
    )
    before, artifacts = await state(past_posts_engine, origin, intent)
    await observations.observe(
        past_posts_engine, origin, src, observations.bundle(attribution="not_detected")
    )
    assert await c.release_stale_stage(
        engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
    )
    released, retained = await state(past_posts_engine, origin, intent)
    assert (
        released["transit_artifact_ref"] is None
        and released["staged_generation_id"] is None
        and released["staged_observation_revision"] is None
    )
    assert (
        released["publish_step"] == "none"
        and released["transit_asset_ref"] is None
        and retained == artifacts
    )
    new = await c.stage(
        engine=past_posts_engine, job=job, intent_id=intent, transit=transit
    )
    after, retained = await state(past_posts_engine, origin, intent)
    assert new != old and len(transit.calls) == 2 and len(retained) == 2
    assert (
        after["capture_observation_revision"] > before["capture_observation_revision"]
    )
    assert all(a["cleanup_state"] == "pending" for a in retained)


async def no_sleep(seconds):
    return None


async def test_actual_past_posts_pipeline_never_uses_drive_fetch_and_copies_both_permits(
    past_posts_engine, origin
):
    module()
    src, item, intent, job, c, calls = await ready(past_posts_engine, origin)
    from src.services.target import publish_pipeline
    from src.services.target.meta_adapter import StubMetaAdapter

    async def drive_forbidden(*args):
        raise AssertionError("past-posts source used Drive fetch")

    transit = Transit()
    meta = StubMetaAdapter()
    result = await publish_pipeline.run_publish_pipeline(
        job,
        engine=past_posts_engine,
        meta=meta,
        transit=transit,
        media_fetch=drive_forbidden,
        past_posts=c,
        sleep=no_sleep,
    )
    assert (
        result == publish_pipeline.POSTED
        and len(transit.calls) == 1
        and len(meta.create_calls) == len(meta.publish_calls) == 1
    )
    async with observations.uow(past_posts_engine, origin) as conn:
        assert (
            await conn.scalar(
                text(
                    "SELECT count(*) FROM instagram_operation_provenance WHERE intent_id=:id"
                ),
                dict(id=intent),
            )
            == 2
        )
        assert (
            await conn.scalar(
                text(
                    "SELECT count(*) FROM instagram_artifacts WHERE intent_id=:id AND outcome='settled' AND cleanup_state='pending'"
                ),
                dict(id=intent),
            )
            == 1
        )


async def test_container_ready_restaging_recreates_container_with_new_generation(
    past_posts_engine, origin
):
    module()
    src, item, intent, job, c, calls = await ready(past_posts_engine, origin)
    transit = Transit()
    oldref = await c.stage(
        engine=past_posts_engine, job=job, intent_id=intent, transit=transit
    )
    first = await permit(past_posts_engine, origin, intent, job)
    async with observations.uow(past_posts_engine, origin) as conn:
        await provider_ops.resolve_permit(
            conn,
            op_id=first["id"],
            outcome="succeeded",
            response_ref={"v": 1, "container_id": "old-confirmed"},
        )
        await conn.execute(
            text(
                "UPDATE post_intents SET ig_container_id='old-confirmed',publish_step='container_ready' WHERE id=:id"
            ),
            dict(id=intent),
        )
        before = dict(
            (
                await conn.execute(
                    text(
                        "SELECT * FROM instagram_operation_provenance WHERE operation_id=:id"
                    ),
                    dict(id=first["id"]),
                )
            )
            .mappings()
            .one()
        )
    await observations.observe(
        past_posts_engine, origin, src, observations.bundle(attribution="not_detected")
    )
    from src.services.target import publish_pipeline
    from src.services.target.meta_adapter import StubMetaAdapter

    async def drive_forbidden(*args):
        raise AssertionError("past-posts source used Drive fetch")

    meta = StubMetaAdapter()
    assert (
        await publish_pipeline.run_publish_pipeline(
            job,
            engine=past_posts_engine,
            meta=meta,
            transit=transit,
            media_fetch=drive_forbidden,
            past_posts=c,
            sleep=no_sleep,
        )
        == publish_pipeline.POSTED
    )
    assert (
        len(transit.calls) == 2
        and transit.calls[-1] != oldref
        and len(meta.create_calls) == 1
    )
    assert meta.publish_calls[0]["container_id"] != "old-confirmed"
    async with observations.uow(past_posts_engine, origin) as conn:
        generations = list(
            (
                await conn.execute(
                    text(
                        "SELECT generation FROM provider_operations WHERE intent_id=:id AND op_kind='container_create' ORDER BY generation"
                    ),
                    dict(id=intent),
                )
            ).scalars()
        )
        retained = dict(
            (
                await conn.execute(
                    text(
                        "SELECT * FROM instagram_operation_provenance WHERE operation_id=:id"
                    ),
                    dict(id=first["id"]),
                )
            )
            .mappings()
            .one()
        )
        assert generations == [1, 2] and retained == before


async def test_caught_permit_refusal_then_outer_commit_cannot_keep_effect_rows(
    past_posts_engine, origin, monkeypatch
):
    module()
    src, item, intent, job, c, calls = await ready(past_posts_engine, origin)
    await c.stage(
        engine=past_posts_engine, job=job, intent_id=intent, transit=Transit()
    )

    async def expired(*args, **kwargs):
        return False

    monkeypatch.setattr(provider_ops, "_lease_is_live", expired)
    from src.services.target.unit_of_work import apply_gucs

    async with past_posts_engine.connect() as conn:
        await apply_gucs(conn, tenant_id=str(origin["ws"]), actor_kind="system")
        with pytest.raises(provider_ops.PermitRefused):
            await provider_ops.acquire_permit(
                conn,
                workspace_id=str(origin["ws"]),
                intent_id=intent,
                job_id=job["id"],
                lease_token=job["lease_token"],
                op_kind="container_create",
                past_posts_capability=foundation.capability(),
            )
        await conn.commit()
    async with observations.uow(past_posts_engine, origin) as conn:
        assert (
            await conn.scalar(
                text("SELECT count(*) FROM provider_operations WHERE intent_id=:id"),
                dict(id=intent),
            )
            == 0
        )
        assert (
            await conn.scalar(
                text(
                    "SELECT count(*) FROM instagram_operation_provenance WHERE intent_id=:id"
                ),
                dict(id=intent),
            )
            == 0
        )


async def test_past_posts_pipeline_missing_coordinator_fails_closed(
    past_posts_engine, origin
):
    module()
    src, item, intent, job, c, calls = await ready(past_posts_engine, origin)
    from src.services.target import publish_pipeline
    from src.services.target.meta_adapter import StubMetaAdapter

    async def forbidden(*args):
        raise AssertionError("uncomposed past-posts source made a media request")

    transit = Transit()
    meta = StubMetaAdapter()
    assert (
        await publish_pipeline.run_publish_pipeline(
            job,
            engine=past_posts_engine,
            meta=meta,
            transit=transit,
            media_fetch=forbidden,
            sleep=no_sleep,
        )
        == publish_pipeline.POISONED
    )
    assert not transit.calls and not meta.create_calls and not meta.publish_calls


async def test_committed_publish_prevents_restaging_and_another_upload(
    past_posts_engine, origin
):
    m = module()
    src, item, intent, job, c, calls = await ready(past_posts_engine, origin)
    transit = Transit()
    await c.stage(engine=past_posts_engine, job=job, intent_id=intent, transit=transit)
    async with observations.uow(past_posts_engine, origin) as conn:
        await conn.execute(
            text(
                "UPDATE post_intents SET ig_container_id='confirmed',publish_step='container_ready' WHERE id=:id"
            ),
            dict(id=intent),
        )
    await permit(past_posts_engine, origin, intent, job, kind="publish")
    with pytest.raises(
        m.PastPostsEffectRefused, match="past_posts_operation_unresolved"
    ):
        await c.release_stale_stage(
            engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
        )
    assert len(transit.calls) == 1


@pytest.mark.parametrize(
    "live_state", ["scheduled", "prompt_pending", "awaiting_approval"]
)
@pytest.mark.parametrize("failure", ["gone", "unsupported"])
async def test_bound_early_card_content_failure_parks_review(
    past_posts_engine, origin, live_state, failure
):
    # A gone slide is proved against the indexed slide count: a carousel.
    src, item, intent = await preparation.selected(
        past_posts_engine, origin, carousel=failure == "gone"
    )
    selected, calls = preparation.service(
        past_posts_engine,
        origin,
        changed_child="456" if failure == "gone" else None,
        content=b"unsupported" if failure == "unsupported" else None,
    )
    async with observations.uow(past_posts_engine, origin) as conn:
        for state_name in ("prompt_pending", "awaiting_approval"):
            if live_state == "scheduled" or (
                live_state == "prompt_pending" and state_name == "awaiting_approval"
            ):
                break
            await conn.execute(
                text("UPDATE post_intents SET state=:state WHERE id=:id"),
                dict(state=state_name, id=intent),
            )
    c = module().PastPostsPipeline(
        capability=foundation.capability(),
        fetch_selected=selected.fetch_selected,
        prepare_selected=selected.selected_prepare,
    )
    await c.selected_prepare(
        engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
    )
    async with observations.uow(past_posts_engine, origin) as conn:
        row = (
            (
                await conn.execute(
                    text("SELECT state,last_error FROM post_intents WHERE id=:id"),
                    dict(id=intent),
                )
            )
            .mappings()
            .one()
        )
        assert row["state"] == "review_required"
        assert row["last_error"]["error"]["reason"] == "past_posts_content_unavailable"
        p = (
            (
                await conn.execute(
                    text(
                        "SELECT selected_asset_identity,selection_observation_revision FROM instagram_intent_provenance WHERE intent_id=:id"
                    ),
                    {"id": intent},
                )
            )
            .mappings()
            .one()
        )
        assert (
            p["selected_asset_identity"] == "123"
            and p["selection_observation_revision"] == 1
        )
        if failure == "gone":
            assert (
                await conn.scalar(
                    text("SELECT state FROM media_items WHERE id=:id"),
                    {"id": item.media_item_id},
                )
                == "removed"
            )
    assert "scontent.cdninstagram.com" not in calls if failure == "gone" else True


#: A slide judged gone as the preparation raises it: the adapter's facts, the
#: turn's counters and the slide count the index recorded.
REFUSAL = {
    "v": 1,
    "kind": "slide_gone",
    "children": 1,
    "complete": True,
    "counters": {
        "graph_attempts": 2,
        "observed_parents": 1,
        "source_fence_drop": 3,
        "listed_parents": 4,
        "cdn_attempts": 5,
    },
    "slide_count": 2,
}


async def parked_after(engine, chain, pending):
    """A bound card parked by a preparation that raises `pending`: the story's
    row, with whether its `last_error` holds an `evidence` key, and the details
    of the audit rows that carry the park's reason."""
    src, item, intent = await preparation.selected(engine, chain)
    selected, _ = preparation.service(engine, chain)
    await selected.selected_prepare(
        engine=engine, workspace_id=str(chain["ws"]), intent_id=intent
    )

    async def refuse(**kwargs):
        raise pending

    c = module().PastPostsPipeline(
        capability=foundation.capability(),
        fetch_selected=selected.fetch_selected,
        prepare_selected=refuse,
    )
    await c.selected_prepare(
        engine=engine, workspace_id=str(chain["ws"]), intent_id=intent
    )
    async with observations.uow(engine, chain) as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT state,last_error,last_error ? 'evidence' AS has_evidence FROM post_intents WHERE id=:id"
                    ),
                    dict(id=intent),
                )
            )
            .mappings()
            .one()
        )
        details = list(
            (
                await conn.execute(
                    text(
                        "SELECT detail FROM audit_events WHERE entity_id=:id AND detail->>'reason'='past_posts_content_unavailable'"
                    ),
                    dict(id=intent),
                )
            ).scalars()
        )
    return row, details


async def test_park_records_the_refusal_evidence_on_the_story_and_in_the_audit(
    past_posts_engine, origin
):
    """The park keeps the evidence the preparation's refusal carried beside its
    reason: the story's `last_error -> 'error' -> 'refusal'` and the audit
    detail's `refusal` are that evidence. Nothing is written under
    `last_error -> 'evidence'`; the gate's workspace has no push binding, so the
    customer-notified latch that shares that key is not written either."""
    row, details = await parked_after(
        past_posts_engine,
        origin,
        source_policy.MediaAdmissionPending("asset_unavailable", evidence=REFUSAL),
    )
    assert row["state"] == "review_required"
    assert row["last_error"] == {
        "v": 1,
        "error": {
            "type": "PastPostsEffectRefused",
            "reason": "past_posts_content_unavailable",
            "message": "past_posts_content_unavailable",
            "refusal": REFUSAL,
        },
    }
    assert row["has_evidence"] is False
    assert details == [{"reason": "past_posts_content_unavailable", "refusal": REFUSAL}]


async def test_park_without_refusal_evidence_writes_no_refusal_key(
    past_posts_engine, origin
):
    """A preparation that raises a pending carrying no evidence parks the story
    with its reason alone: neither the story's `last_error -> 'error'` nor the
    audit detail has a `refusal` key."""
    row, details = await parked_after(
        past_posts_engine,
        origin,
        source_policy.MediaAdmissionPending("asset_unavailable"),
    )
    assert row["state"] == "review_required"
    assert row["last_error"] == {
        "v": 1,
        "error": {
            "type": "PastPostsEffectRefused",
            "reason": "past_posts_content_unavailable",
            "message": "past_posts_content_unavailable",
        },
    }
    assert row["has_evidence"] is False
    assert details == [{"reason": "past_posts_content_unavailable"}]


async def test_truncated_missing_slide_defers_early_card_without_review(
    past_posts_engine, origin
):
    src, item, intent = await preparation.selected(past_posts_engine, origin)
    selected, calls = preparation.service(
        past_posts_engine,
        origin,
        changed_child="456",
        truncated=True,
        retry=preparation.RETRY,
    )
    c = module().PastPostsPipeline(
        capability=foundation.capability(),
        fetch_selected=selected.fetch_selected,
        prepare_selected=selected.selected_prepare,
    )
    with pytest.raises(source_policy.MediaAdmissionPending) as error:
        await c.selected_prepare(
            engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
        )
    assert error.value.reason == "past_posts_revalidation_required"
    async with observations.uow(past_posts_engine, origin) as conn:
        row = (
            (
                await conn.execute(
                    text("SELECT state FROM post_intents WHERE id=:id"), {"id": intent}
                )
            )
            .mappings()
            .one()
        )
    assert row["state"] == "scheduled"
    assert calls == ["graph.instagram.com"] * 2


async def test_early_card_source_off_is_deferred_without_review(
    past_posts_engine, origin
):
    from src.services.target import source_read_admission as admission

    src, item, intent = await preparation.selected(past_posts_engine, origin)
    selected, calls = preparation.service(past_posts_engine, origin)
    await off(past_posts_engine, origin, src)
    c = module().PastPostsPipeline(
        capability=foundation.capability(),
        fetch_selected=selected.fetch_selected,
        prepare_selected=selected.selected_prepare,
    )
    with pytest.raises(admission.SourceReadRefused, match="source_off"):
        await c.selected_prepare(
            engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
        )
    async with observations.uow(past_posts_engine, origin) as conn:
        assert (
            await conn.scalar(
                text("SELECT state FROM post_intents WHERE id=:id"), dict(id=intent)
            )
            == "scheduled"
        )
    assert not calls


async def test_early_card_cancel_overlay_never_parks_review(past_posts_engine, origin):
    from src.services.target import source_policy

    src, item, intent = await preparation.selected(past_posts_engine, origin)
    selected, calls = preparation.service(past_posts_engine, origin)
    await selected.selected_prepare(
        engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
    )
    async with observations.uow(past_posts_engine, origin) as conn:
        await conn.execute(
            text("UPDATE post_intents SET cancel_requested=true WHERE id=:id"),
            dict(id=intent),
        )
    c = module().PastPostsPipeline(
        capability=foundation.capability(),
        fetch_selected=selected.fetch_selected,
        prepare_selected=selected.selected_prepare,
    )
    with pytest.raises(source_policy.MediaAdmissionPending, match="asset_unavailable"):
        await c.selected_prepare(
            engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
        )
    async with observations.uow(past_posts_engine, origin) as conn:
        assert (
            await conn.scalar(
                text("SELECT state FROM post_intents WHERE id=:id"), dict(id=intent)
            )
            == "scheduled"
        )


@pytest.mark.parametrize("race", ["reobserve", "generation", "off_on"])
async def test_early_card_failure_cannot_cross_captured_version(
    past_posts_engine, origin, race
):
    from src.services.target import source_policy
    from src.services.target.source_read_admission import SourceReadRefused

    # A carousel, so the slide the reads lack is proved gone: a content failure.
    src, item, intent = await preparation.selected(
        past_posts_engine, origin, carousel=True
    )
    selected, calls = preparation.service(
        past_posts_engine, origin, changed_child="456"
    )

    async def fail_then_change(**kwargs):
        try:
            await selected.selected_prepare(**kwargs)
        except source_policy.MediaAdmissionPending:
            if race == "reobserve":
                await observations.observe(
                    past_posts_engine,
                    origin,
                    src,
                    observations.bundle(attribution="not_detected"),
                )
            elif race == "generation":
                await foundation.ensure(
                    past_posts_engine, origin, foundation.authorization()
                )
            else:
                await off(past_posts_engine, origin, src)
                async with observations.uow(past_posts_engine, origin) as conn:
                    await conn.execute(
                        text(
                            "UPDATE category_post_case_mix SET ratio=1 WHERE source_id=:id AND effective_to IS NULL"
                        ),
                        dict(id=src["source_id"]),
                    )
                    await conn.execute(
                        text(
                            "UPDATE media_sources SET source_use_epoch=source_use_epoch+2 WHERE id=:id"
                        ),
                        dict(id=src["source_id"]),
                    )
            raise

    c = module().PastPostsPipeline(
        capability=foundation.capability(),
        fetch_selected=selected.fetch_selected,
        prepare_selected=fail_then_change,
    )
    with pytest.raises((SourceReadRefused, source_policy.MediaAdmissionPending)):
        await c.selected_prepare(
            engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
        )
    async with observations.uow(past_posts_engine, origin) as conn:
        row = (
            (
                await conn.execute(
                    text("SELECT state,last_error FROM post_intents WHERE id=:id"),
                    dict(id=intent),
                )
            )
            .mappings()
            .one()
        )
        assert row["state"] == "scheduled" and row["last_error"] is None
        assert (
            await conn.scalar(
                text(
                    "SELECT count(*) FROM audit_events WHERE entity_id=:id AND detail->>'reason' IN ('past_posts_content_changed','past_posts_content_unavailable')"
                ),
                dict(id=intent),
            )
            == 0
        )
    p = await preparation.proof(past_posts_engine, origin, intent)
    assert (
        p["selected_asset_identity"] == "123"
        and str(p["selection_generation_id"]) == src["generation_id"]
        and p["selection_observation_revision"] == 1
    )
    assert calls == ["graph.instagram.com"] * 4


@pytest.mark.parametrize("during_preparation", [False, True])
async def test_approved_past_posts_cancel_refunds_before_content_review(
    past_posts_engine, origin, during_preparation
):
    from src.services.target import publish_pipeline
    from src.services.target.meta_adapter import StubMetaAdapter

    src, item, intent, job, c, calls = await ready(past_posts_engine, origin)
    async with observations.uow(past_posts_engine, origin) as conn:
        await conn.execute(
            text(
                "UPDATE post_intents SET state='approved',cancel_requested=:flag WHERE id=:id"
            ),
            dict(id=intent, flag=not during_preparation),
        )
        assert (
            await conn.scalar(
                text(
                    "SELECT count FROM daily_post_counts WHERE workspace_id=:ws AND ig_account_id=:account AND local_date=current_date"
                ),
                dict(ws=str(origin["ws"]), account=str(origin["iga"])),
            )
            == 1
        )
    async with observations.uow(past_posts_engine, origin) as conn:
        cap_date = await conn.scalar(
            text("SELECT cap_consumed_on FROM post_intents WHERE id=:id"),
            dict(id=intent),
        )
    entered = []
    actual_prepare = c.prepare_capture

    async def prepare_then_cancel(**kwargs):
        entered.append(True)
        assert during_preparation, (
            "pre-existing cancellation must skip selected preparation"
        )
        async with observations.uow(past_posts_engine, origin) as conn:
            await conn.execute(
                text("UPDATE post_intents SET cancel_requested=true WHERE id=:id"),
                dict(id=intent),
            )
        await actual_prepare(**kwargs)

    c.prepare_capture = prepare_then_cancel

    async def forbidden_fetch(subject):
        pytest.fail("cancelled past-posts intent cannot fetch media")

    transit = Transit()
    meta = StubMetaAdapter()
    assert (
        await publish_pipeline.run_publish_pipeline(
            job,
            engine=past_posts_engine,
            meta=meta,
            transit=transit,
            media_fetch=forbidden_fetch,
            past_posts=c,
        )
        == publish_pipeline.CANCELLED
    )
    async with observations.uow(past_posts_engine, origin) as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT state,cap_consumed_on,last_error FROM post_intents WHERE id=:id"
                    ),
                    dict(id=intent),
                )
            )
            .mappings()
            .one()
        )
        assert (
            row["state"] == "cancelled"
            and row["cap_consumed_on"] == cap_date
            and row["last_error"] is None
        )
        assert (
            await conn.scalar(
                text(
                    "SELECT count FROM daily_post_counts WHERE workspace_id=:ws AND ig_account_id=:account AND local_date=current_date"
                ),
                dict(ws=str(origin["ws"]), account=str(origin["iga"])),
            )
            == 0
        )
        assert (
            await conn.scalar(
                text("SELECT state FROM jobs WHERE id=:id"), dict(id=job["id"])
            )
            == "cancelled"
        )
        assert (
            await conn.scalar(
                text("SELECT count(*) FROM provider_operations WHERE intent_id=:id"),
                dict(id=intent),
            )
            == 0
        )
    assert (
        bool(entered) == during_preparation
        and not transit.calls
        and not meta.create_calls
        and not meta.publish_calls
    )
