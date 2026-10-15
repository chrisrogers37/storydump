"""Selected-only preparation through actual tenant roles, real local JPEG proof."""

from dataclasses import replace
from datetime import datetime, timezone
from io import BytesIO
import json
import uuid

import httpx
import pytest
from PIL import Image
from sqlalchemy import text

from src.services.target import egress, media_adapters, media_validation, source_policy
from src.services.target import source_read_admission as admission
from src.services.target.instagram_archive_adapter import (
    ArchiveAccess,
    InstagramArchiveAdapter,
)
from src.services.target.unit_of_work import in_transaction
from tests.scripts import test_candidate_selection_gate as selection
from tests.scripts import test_instagram_observations_gate as obs
from tests.scripts import test_instagram_sources_gate as foundation

archive_engine = foundation.archive_engine
origin = foundation.origin
pytestmark = pytest.mark.integration


def jpeg(color="red"):
    output = BytesIO()
    Image.new("RGB", (64, 128), color).save(output, format="JPEG")
    return output.getvalue()


async def selected(engine, chain):
    async with obs.uow(engine, chain) as conn:
        await conn.execute(
            text("UPDATE ig_accounts SET provider_account_ref=:ref WHERE id=:id"),
            {
                "id": str(chain["iga"]),
                "ref": str(int(uuid.UUID(str(chain["iga"])).int % 10**16) + 10**16),
            },
        )
    src, item = await selection.metadata(engine, chain)
    async with obs.uow(engine, chain) as conn:
        intent = str(
            (
                await conn.execute(
                    text(
                        "INSERT INTO post_intents(workspace_id,ig_account_id,media_item_id,provider_account_ref,approval_mode,schedule_slot_at) "
                        "VALUES(:ws,:acct,:item,'synthetic','manual',now()) RETURNING id"
                    ),
                    {
                        "ws": str(chain["ws"]),
                        "acct": str(chain["iga"]),
                        "item": item.media_item_id,
                    },
                )
            ).scalar_one()
        )
    return src, item, intent


async def proof(engine, chain, intent):
    async with obs.uow(engine, chain) as conn:
        return (
            (
                await conn.execute(
                    text(
                        "SELECT p.*,a.validation_state,a.validation_capture FROM instagram_intent_provenance p "
                        "LEFT JOIN instagram_media_assets a ON a.workspace_id=p.workspace_id AND a.source_id=p.source_id AND a.media_item_id=p.media_item_id WHERE p.intent_id=:id"
                    ),
                    {"id": intent},
                )
            )
            .mappings()
            .first()
        )


def service(
    engine,
    chain,
    *,
    content=None,
    before=None,
    returned=None,
    changed_child=None,
    truncated=False,
):
    from src.services.target.instagram_preparation import (
        ArchivePreparationPorts,
        SelectedPreparation,
    )

    calls = []
    data = jpeg() if content is None else content

    async def prepare(capture, config):
        assert not in_transaction()
        if before:
            await before()

        async def access(source_id, *, workspace_id):
            return ArchiveAccess(capture.account_ref, "synthetic-token", capture)

        async def handler(request):
            assert not in_transaction()
            calls.append(request.url.host)
            if returned:
                await returned(request)
            if request.url.host == "graph.instagram.com":
                if request.url.path.endswith("/children"):
                    return httpx.Response(
                        200,
                        json={
                            "data": [
                                {
                                    "id": changed_child,
                                    "media_type": "IMAGE",
                                    "media_url": "https://scontent-test.cdninstagram.com/selected.jpg",
                                }
                            ],
                            **(
                                {"paging": {"next": "https://graph.instagram.com/next"}}
                                if truncated
                                else {}
                            ),
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "id": "123",
                        "media_type": "CAROUSEL_ALBUM" if changed_child else "IMAGE",
                        "owner": {"id": capture.account_ref},
                        "media_url": "https://scontent-test.cdninstagram.com/selected.jpg",
                    },
                )
            return httpx.Response(
                200, content=data, headers={"Content-Type": "image/jpeg"}
            )

        return InstagramArchiveAdapter(
            access_provider=access,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            graph_version="v26.0",
            policy=egress.EgressPolicy().without(enforce_private_address_block=False),
            admission_provider=admission.admission_provider(
                engine, capability=foundation.capability()
            ),
        )

    return SelectedPreparation(
        ArchivePreparationPorts(foundation.capability(), prepare, obs.keys())
    ), calls


async def test_selected_bytes_bind_before_dispatch_and_real_proof_is_strict(
    archive_engine, origin, monkeypatch
):
    src, item, intent = await selected(archive_engine, origin)

    validations = []
    validate = media_validation.validate_media

    async def measured(value, **kwargs):
        validations.append(value.data)
        return await validate(value, **kwargs)

    monkeypatch.setattr(media_validation, "validate_media", measured)

    async def verify_bound(request):
        assert await proof(archive_engine, origin, intent) is not None

    prepared, calls = service(archive_engine, origin, returned=verify_bound)
    result = await prepared.fetch_selected(
        engine=archive_engine, workspace_id=str(origin["ws"]), intent_id=intent
    )
    assert result.content == jpeg() and result.validation.state == "supported"
    assert result.validation.selected_asset_id == "123"
    assert (result.counters.graph_attempts, result.counters.cdn_attempts) == (1, 1)
    row = await proof(archive_engine, origin, intent)
    assert (
        row["selected_asset_identity"] == "123"
        and row["validation_fingerprint"] == result.validation.fingerprint
    )
    async with obs.uow(archive_engine, origin) as conn:
        verdict = await source_policy.intent_suitability(
            conn,
            workspace_id=str(origin["ws"]),
            intent_id=intent,
            context=await source_policy.suitability_context(
                conn, workspace_id=str(origin["ws"]), capability=foundation.capability()
            ),
        )
        assert verdict["suitable"]
    await prepared.selected_prepare(
        engine=archive_engine, workspace_id=str(origin["ws"]), intent_id=intent
    )
    assert len(calls) == 2, (
        "coherent preparation short-circuits; card reads separately verify served bytes"
    )
    again = await prepared.fetch_selected(
        engine=archive_engine, workspace_id=str(origin["ws"]), intent_id=intent
    )
    assert (
        len(calls) == 4
        and again.validation.fingerprint == result.validation.fingerprint
    )
    assert "synthetic-token" not in repr(result) and repr(result.content) not in repr(
        result
    )
    from src.services.target import prompts
    from src import worker

    async with obs.uow(archive_engine, origin) as conn:
        conn.info["archive_capability"] = foundation.capability()
        card = await prompts.rerender_prompt(conn, intent_id=intent)
    assert card["media"]["intent_id"] == intent
    data, _, mime = await worker._card_media_fetch(
        archive_engine, {}, fetch_selected=prepared.fetch_selected
    )(card["media"])
    assert data == jpeg() and mime == "image/jpeg" and len(calls) == 6
    assert validations == [jpeg()] * 3, "each returned byte stream is validated once"


async def test_second_slide_prepares_its_own_identity_and_bytes(archive_engine, origin):
    from src.services.target.instagram_catalog import ArchiveAsset

    src, _, _ = await selected(archive_engine, origin)
    await obs.observe(
        archive_engine,
        origin,
        src,
        replace(
            obs.bundle(attribution="not_detected"),
            assets=(
                ArchiveAsset(selected_asset_id="123", media_kind="image", position=1),
                ArchiveAsset(selected_asset_id="456", media_kind="image", position=2),
            ),
            slide_count=2,
        ),
    )
    async with obs.uow(archive_engine, origin) as conn:
        slide_item = await conn.scalar(
            text(
                "SELECT media_item_id FROM instagram_media_assets WHERE workspace_id=:ws AND source_id=:sid AND asset_media_id='456'"
            ),
            {"ws": str(origin["ws"]), "sid": src["source_id"]},
        )
        intent = str(
            await conn.scalar(
                text(
                    "INSERT INTO post_intents(workspace_id,ig_account_id,media_item_id,provider_account_ref,approval_mode,schedule_slot_at) "
                    "VALUES(:ws,:acct,:item,'synthetic','manual',now()) RETURNING id"
                ),
                {
                    "ws": str(origin["ws"]),
                    "acct": str(origin["iga"]),
                    "item": str(slide_item),
                },
            )
        )
    prepared, calls = service(archive_engine, origin, changed_child="456")
    result = await prepared.fetch_selected(
        engine=archive_engine, workspace_id=str(origin["ws"]), intent_id=intent
    )
    assert result.validation.selected_asset_id == "456"
    assert result.content == jpeg()
    assert calls == ["graph.instagram.com"] * 2 + ["scontent-test.cdninstagram.com"]
    row = await proof(archive_engine, origin, intent)
    assert row["selected_asset_identity"] == "456"
    assert row["validation_state"] == "supported"


async def test_removed_slide_cannot_replace_bound_card(archive_engine, origin):
    src, item, intent = await selected(archive_engine, origin)
    prepared, calls = service(archive_engine, origin)
    await prepared.selected_prepare(
        engine=archive_engine, workspace_id=str(origin["ws"]), intent_id=intent
    )
    from src.services.target.instagram_catalog import ArchiveAsset

    await obs.observe(
        archive_engine,
        origin,
        src,
        replace(
            obs.bundle(attribution="not_detected"),
            assets=(ArchiveAsset(selected_asset_id="456", media_kind="image"),),
            slide_count=1,
        ),
    )
    with pytest.raises(source_policy.MediaAdmissionPending) as err:
        await prepared.fetch_selected(
            engine=archive_engine, workspace_id=str(origin["ws"]), intent_id=intent
        )
    assert err.value.reason == "asset_unavailable" and len(calls) == 2
    async with obs.uow(archive_engine, origin) as conn:
        assert (
            await conn.scalar(
                text("SELECT state FROM media_items WHERE id=:id"),
                {"id": item.media_item_id},
            )
            == "removed"
        )
        assert (
            await conn.scalar(
                text(
                    "SELECT count(*) FROM instagram_media_assets WHERE media_item_id=:id"
                ),
                {"id": item.media_item_id},
            )
            == 0
        )
        assert (
            await conn.scalar(
                text(
                    "SELECT selected_asset_identity FROM instagram_intent_provenance WHERE intent_id=:id"
                ),
                {"id": intent},
            )
            == "123"
        )


@pytest.mark.parametrize("when", ["before_dispatch", "after_graph", "before_cas"])
async def test_off_fences_selected_work_without_landing_proof(
    archive_engine, origin, monkeypatch, when
):
    src, item, intent = await selected(archive_engine, origin)

    async def off():
        async with obs.uow(archive_engine, origin) as conn:
            await conn.execute(
                text(
                    "UPDATE category_post_case_mix SET ratio=0 WHERE source_id=:sid AND effective_to IS NULL"
                ),
                {"sid": src["source_id"]},
            )

    async def returned(request):
        if request.url.host == "graph.instagram.com":
            await off()

    if when == "before_cas":
        validate = media_validation.validate_media

        async def race(value, **kwargs):
            result = await validate(value, **kwargs)
            await off()
            return result

        monkeypatch.setattr(media_validation, "validate_media", race)
    prepared, calls = service(
        archive_engine,
        origin,
        before=off if when == "before_dispatch" else None,
        returned=returned if when == "after_graph" else None,
    )
    with pytest.raises(
        (admission.SourceReadRefused, media_adapters.MediaSourceFenceDrop)
    ):
        await prepared.fetch_selected(
            engine=archive_engine, workspace_id=str(origin["ws"]), intent_id=intent
        )
    assert len(calls) == {"before_dispatch": 0, "after_graph": 1, "before_cas": 2}[when]
    row = await proof(archive_engine, origin, intent)
    assert (
        row["validation_fingerprint"] is None and row["validation_state"] == "pending"
    )


async def test_observation_race_refuses_cas_and_keeps_historical_selection(
    archive_engine, origin, monkeypatch
):
    src, item, intent = await selected(archive_engine, origin)
    validate = media_validation.validate_media

    async def race(value, **kwargs):
        result = await validate(value, **kwargs)
        await obs.observe(
            archive_engine,
            origin,
            src,
            obs.bundle(attribution="not_detected", like_count=2),
        )
        return result

    monkeypatch.setattr(media_validation, "validate_media", race)
    prepared, calls = service(archive_engine, origin)
    with pytest.raises(source_policy.MediaAdmissionPending) as err:
        await prepared.fetch_selected(
            engine=archive_engine, workspace_id=str(origin["ws"]), intent_id=intent
        )
    assert err.value.reason == "archive_revalidation_required"
    row = await proof(archive_engine, origin, intent)
    assert (
        row["selection_observation_revision"] == 1
        and row["validation_fingerprint"] is None
    )


async def test_unsupported_selected_bytes_become_coherent_candidate_exclusion(
    archive_engine, origin
):
    src, item, intent = await selected(archive_engine, origin)
    prepared, calls = service(archive_engine, origin, content=b"corrupt JPEG")
    with pytest.raises(source_policy.MediaAdmissionPending) as err:
        await prepared.selected_prepare(
            engine=archive_engine, workspace_id=str(origin["ws"]), intent_id=intent
        )
    assert err.value.reason == "unsupported"
    row = await proof(archive_engine, origin, intent)
    assert (
        row["validation_state"] == "unsupported"
        and row["validation_capture"]["state"] == "unsupported"
    )
    async with obs.uow(archive_engine, origin) as conn:
        assert (await selection.verdict(conn, origin, src, intent=intent))[
            "reason"
        ] == "unsupported"


async def test_new_grant_after_response_fences_existing_selected_capture(
    archive_engine, origin
):
    src, item, intent = await selected(archive_engine, origin)

    async def renew(request):
        if request.url.host == "graph.instagram.com":
            await foundation.ensure(archive_engine, origin, foundation.authorization())

    prepared, calls = service(archive_engine, origin, returned=renew)
    with pytest.raises(media_adapters.MediaSourceFenceDrop) as err:
        await prepared.fetch_selected(
            engine=archive_engine, workspace_id=str(origin["ws"]), intent_id=intent
        )
    assert err.value.reason == "source_generation_changed" and len(calls) == 1
    row = await proof(archive_engine, origin, intent)
    assert (
        row["validation_fingerprint"] is None
        and str(row["selection_generation_id"]) == src["generation_id"]
    )


async def test_staged_capture_cannot_change_proof_even_when_same_asset_bytes_change(
    archive_engine, origin
):
    import uuid
    from src.services.target import instagram_artifacts

    src, item, intent = await selected(archive_engine, origin)
    prepared, calls = service(archive_engine, origin)
    original = await prepared.fetch_selected(
        engine=archive_engine, workspace_id=str(origin["ws"]), intent_id=intent
    )
    async with obs.uow(archive_engine, origin) as conn:
        row = await source_policy.intent_suitability(
            conn,
            workspace_id=str(origin["ws"]),
            intent_id=intent,
            context=await source_policy.suitability_context(
                conn, workspace_id=str(origin["ws"]), capability=foundation.capability()
            ),
        )
        ref = await instagram_artifacts.admit_upload(
            conn,
            workspace_id=str(origin["ws"]),
            source_id=src["source_id"],
            data_generation_id=src["generation_id"],
            observation_revision=1,
            media_item_id=item.media_item_id,
            intent_id=intent,
            capture_epoch=original.capture.source_use_epoch,
            policy_revision=row["policy_revision"],
            selected_asset_identity="123",
            validation_fingerprint=original.validation.fingerprint,
            admitted_asset_ref=f"ws/{origin['ws']}/" + uuid.uuid4().hex,
            admitted_resource_type="image",
            upload_attempt_id=str(uuid.uuid4()),
        )
        await conn.execute(
            text(
                "UPDATE instagram_intent_provenance SET transit_artifact_ref=:ref,staged_generation_id=:gid,staged_observation_revision=1 WHERE intent_id=:id"
            ),
            {"ref": ref, "gid": src["generation_id"], "id": intent},
        )
    altered, _ = service(archive_engine, origin, content=jpeg("blue"))
    with pytest.raises(source_policy.MediaAdmissionPending) as err:
        await altered.fetch_selected(
            engine=archive_engine, workspace_id=str(origin["ws"]), intent_id=intent
        )
    assert err.value.reason == "archive_revalidation_required"
    row = await proof(archive_engine, origin, intent)
    assert (
        row["validation_fingerprint"] == original.validation.fingerprint
        and str(row["transit_artifact_ref"]) == ref
    )


async def test_transient_selected_failure_preserves_metadata_and_permits_retry(
    archive_engine, origin
):
    from src.services.target.instagram_preparation import (
        ArchivePreparationPorts,
        SelectedPreparation,
    )

    src, item, intent = await selected(archive_engine, origin)

    class TransientAdapter:
        async def fetch_bytes(self, *args, **kwargs):
            kwargs["on_counters"](media_adapters.MediaTurnCounters(graph_attempts=1))
            raise media_adapters.MediaRetryableError("synthetic")

    async def prepare(capture, config):
        return TransientAdapter()

    transient = SelectedPreparation(
        ArchivePreparationPorts(foundation.capability(), prepare, obs.keys())
    )
    with pytest.raises(media_adapters.MediaRetryableError):
        await transient.selected_prepare(
            engine=archive_engine, workspace_id=str(origin["ws"]), intent_id=intent
        )
    row = await proof(archive_engine, origin, intent)
    assert row["validation_capture"]["reason"] == "download_pending"
    async with obs.uow(archive_engine, origin) as conn:
        assert (await selection.verdict(conn, origin, src, intent=intent))["suitable"]
    prepared, calls = service(archive_engine, origin)
    await prepared.selected_prepare(
        engine=archive_engine, workspace_id=str(origin["ws"]), intent_id=intent
    )
    assert (
        len(calls) == 2
        and (await proof(archive_engine, origin, intent))["validation_state"]
        == "supported"
    )


async def test_cancel_requested_before_validation_cas_refuses_selected_bytes(
    archive_engine, origin, monkeypatch
):
    src, item, intent = await selected(archive_engine, origin)
    validate = media_validation.validate_media

    async def cancel(value, **kwargs):
        result = await validate(value, **kwargs)
        async with obs.uow(archive_engine, origin) as conn:
            await conn.execute(
                text("UPDATE post_intents SET cancel_requested=true WHERE id=:id"),
                {"id": intent},
            )
        return result

    monkeypatch.setattr(media_validation, "validate_media", cancel)
    prepared, calls = service(archive_engine, origin)
    with pytest.raises(source_policy.MediaAdmissionPending):
        await prepared.fetch_selected(
            engine=archive_engine, workspace_id=str(origin["ws"]), intent_id=intent
        )
    assert (await proof(archive_engine, origin, intent))[
        "validation_fingerprint"
    ] is None


async def test_remote_changed_first_asset_is_coherent_unavailable_without_cdn(
    archive_engine, origin
):
    from src.services.target.instagram_catalog import ArchiveAsset

    src, item, intent = await selected(archive_engine, origin)
    carousel = replace(
        obs.bundle(attribution="not_detected"),
        assets=(
            ArchiveAsset(selected_asset_id="123", media_kind="image", position=1),
            ArchiveAsset(selected_asset_id="456", media_kind="image", position=2),
        ),
        slide_count=2,
    )
    await obs.observe(archive_engine, origin, src, carousel)

    async def keep_lock():
        async with obs.uow(archive_engine, origin) as conn:
            await conn.execute(
                text(
                    "INSERT INTO post_locks(workspace_id,media_item_id,kind) VALUES(:ws,:item,'hold')"
                ),
                {"ws": str(origin["ws"]), "item": item.media_item_id},
            )

    prepared, calls = service(
        archive_engine, origin, changed_child="456", before=keep_lock
    )
    with pytest.raises(source_policy.MediaAdmissionPending) as error:
        await prepared.fetch_selected(
            engine=archive_engine, workspace_id=str(origin["ws"]), intent_id=intent
        )
    assert error.value.reason == "asset_unavailable"
    assert calls == ["graph.instagram.com"] * 2
    async with obs.uow(archive_engine, origin) as conn:
        assert (
            await conn.scalar(
                text("SELECT state FROM media_items WHERE id=:id"),
                {"id": item.media_item_id},
            )
            == "removed"
        )
        assert (
            await conn.scalar(
                text(
                    "SELECT count(*) FROM instagram_media_assets WHERE media_item_id=:id"
                ),
                {"id": item.media_item_id},
            )
            == 0
        )
        assert (
            await conn.scalar(
                text(
                    "SELECT count(*) FROM post_locks WHERE media_item_id=:id AND kind='hold'"
                ),
                {"id": item.media_item_id},
            )
            == 1
        )
        assert (
            await conn.scalar(
                text(
                    "SELECT count(*) FROM instagram_item_tombstones WHERE media_item_id=:id"
                ),
                {"id": item.media_item_id},
            )
            == 1
        )
        assert (
            await conn.scalar(
                text("SELECT count(*) FROM post_intents WHERE id=:id"), {"id": intent}
            )
            == 1
        )
        assert (
            await conn.scalar(
                text(
                    "SELECT count(*) FROM instagram_media_assets WHERE source_id=:sid AND asset_media_id='456'"
                ),
                {"sid": src["source_id"]},
            )
            == 1
        )
    await obs.observe(archive_engine, origin, src, carousel)
    async with obs.uow(archive_engine, origin) as conn:
        assert (
            await conn.scalar(
                text("SELECT state FROM media_items WHERE id=:id"),
                {"id": item.media_item_id},
            )
            == "available"
        )
        assert (
            str(
                await conn.scalar(
                    text(
                        "SELECT media_item_id FROM instagram_media_assets WHERE source_id=:sid AND asset_media_id='123'"
                    ),
                    {"sid": src["source_id"]},
                )
            )
            == item.media_item_id
        )


async def test_truncated_children_missing_selected_slide_defers_without_cdn(
    archive_engine, origin
):
    src, item, intent = await selected(archive_engine, origin)
    prepared, calls = service(
        archive_engine, origin, changed_child="456", truncated=True
    )
    with pytest.raises(source_policy.MediaAdmissionPending) as error:
        await prepared.fetch_selected(
            engine=archive_engine, workspace_id=str(origin["ws"]), intent_id=intent
        )
    assert error.value.reason == "archive_revalidation_required"
    assert calls == ["graph.instagram.com"] * 2
    row = await proof(archive_engine, origin, intent)
    assert row["validation_capture"] is None
    async with obs.uow(archive_engine, origin) as conn:
        assert (
            await conn.scalar(
                text("SELECT state FROM media_items WHERE id=:id"),
                {"id": item.media_item_id},
            )
            == "available"
        )
        assert (
            await conn.scalar(
                text(
                    "SELECT count(*) FROM instagram_media_assets WHERE media_item_id=:id"
                ),
                {"id": item.media_item_id},
            )
            == 1
        )
        assert (
            await conn.scalar(
                text(
                    "SELECT count(*) FROM instagram_item_tombstones WHERE media_item_id=:id"
                ),
                {"id": item.media_item_id},
            )
            == 0
        )


@pytest.mark.parametrize("stale", ["reobserved", "policy", "cancelled"])
async def test_selected_gone_fresh_cas_refuses_stale_or_cancelled_turn(
    archive_engine, origin, stale
):
    src, item, intent = await selected(archive_engine, origin)

    async def change_while_fetching():
        if stale == "reobserved":
            await obs.observe(
                archive_engine,
                origin,
                src,
                obs.bundle(attribution="not_detected"),
            )
            return
        async with obs.uow(archive_engine, origin) as conn:
            if stale == "policy":
                await conn.execute(
                    text(
                        "UPDATE instagram_source_policies SET revision=revision+1 WHERE source_id=:sid"
                    ),
                    {"sid": src["source_id"]},
                )
            else:
                await conn.execute(
                    text("UPDATE post_intents SET cancel_requested=true WHERE id=:id"),
                    {"id": intent},
                )

    prepared, calls = service(
        archive_engine, origin, changed_child="456", before=change_while_fetching
    )
    with pytest.raises(source_policy.MediaAdmissionPending) as error:
        await prepared.fetch_selected(
            engine=archive_engine, workspace_id=str(origin["ws"]), intent_id=intent
        )
    assert error.value.reason == "archive_revalidation_required"
    assert calls == ["graph.instagram.com"] * 2
    async with obs.uow(archive_engine, origin) as conn:
        assert (
            await conn.scalar(
                text("SELECT state FROM media_items WHERE id=:id"),
                {"id": item.media_item_id},
            )
            == "available"
        )
        assert (
            await conn.scalar(
                text(
                    "SELECT count(*) FROM instagram_media_assets WHERE media_item_id=:id"
                ),
                {"id": item.media_item_id},
            )
            == 1
        )
        assert (
            await conn.scalar(
                text(
                    "SELECT count(*) FROM instagram_item_tombstones WHERE media_item_id=:id"
                ),
                {"id": item.media_item_id},
            )
            == 0
        )


async def approved_archive_card(engine, chain):
    from src.services.target import auto_approval, intent_ledger, outbox, prompts

    src, item, intent = await selected(engine, chain)
    prepared, calls = service(engine, chain)
    await prepared.selected_prepare(
        engine=engine, workspace_id=str(chain["ws"]), intent_id=intent
    )
    async with obs.uow(engine, chain) as conn:
        conn.info["archive_capability"] = foundation.capability()
        await source_policy.suitability_context(
            conn, workspace_id=str(chain["ws"]), capability=foundation.capability()
        )
        await conn.execute(
            text(
                "UPDATE workspaces SET api_publishing_enabled=true,approval_mode='auto' WHERE id=:ws"
            ),
            {"ws": str(chain["ws"])},
        )
        await intent_ledger.transition(conn, intent, "prompt_pending")
        await intent_ledger.transition(conn, intent, "awaiting_approval")
        assert (
            await auto_approval.approve_if_due(
                conn, workspace_id=str(chain["ws"]), intent_id=intent
            )
            == "auto"
        )
        binding = str(
            (
                await conn.execute(
                    text(
                        "INSERT INTO channel_bindings(workspace_id,channel,external_ref) VALUES(:ws,'telegram_group',:ref) RETURNING id"
                    ),
                    {"ws": str(chain["ws"]), "ref": "synthetic-" + uuid.uuid4().hex},
                )
            ).scalar_one()
        )
        card = await prompts.rerender_prompt(conn, intent_id=intent)
        outbox_id = await outbox.enqueue(
            conn,
            workspace_id=str(chain["ws"]),
            binding_id=binding,
            kind="approval_prompt",
            intent_id=intent,
            payload=card,
        )
    return prepared, calls, intent, binding, outbox_id


async def post_archive_card(engine, chain, intent, *, via="api"):
    # The same complete ledger updates used by the existing auto-approval gate;
    # no provider call is made, and real transition/audit guards remain active.
    async with obs.uow(engine, chain) as conn:
        await source_policy.suitability_context(
            conn, workspace_id=str(chain["ws"]), capability=foundation.capability()
        )
        await conn.execute(
            text(
                "UPDATE post_intents SET state='publishing',cap_consumed_on=current_date,"
                "publish_step='publish_called',ig_container_id='synthetic-container' WHERE id=:id"
            ),
            {"id": intent},
        )
        await conn.execute(
            text(
                "UPDATE post_intents SET state='posted',published_via=:via,"
                "publish_step='effect_confirmed' WHERE id=:id"
            ),
            {"id": intent, "via": via},
        )


@pytest.mark.parametrize("via", ["api", "dry_run"])
async def test_posted_rule_archive_card_sends_text_outcome_without_selected_fetch(
    archive_engine, origin, via
):
    from src.channels.telegram_transport import TelegramTransport
    from src.services.target import outbox, prompts

    prepared, calls, intent, binding, oid = await approved_archive_card(
        archive_engine, origin
    )
    await post_archive_card(archive_engine, origin, intent, via=via)
    async with obs.uow(archive_engine, origin) as conn:
        card = await prompts.rerender_prompt(conn, intent_id=intent)
        assert (
            "media" not in card and "caption" not in card and "reply_markup" not in card
        )
        assert ("Dry run" if via == "dry_run" else "Posted by YOLO") in card[
            "outcome_text"
        ]
    sent = []

    async def no_fetch(media):
        raise AssertionError("terminal archive outcome must never fetch selected bytes")

    def handler(request):
        sent.append((request.url.path, json.loads(request.read())))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 987}})

    transport = TelegramTransport(
        "synthetic-token",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        media_fetch=no_fetch,
    )

    def factory():
        return obs.uow(archive_engine, origin)

    poller = outbox.OutboxPoller(
        factory,
        binding_id=binding,
        transport=transport.for_chat("synthetic-chat"),
        clock=lambda: datetime.now(timezone.utc),
        interval_seconds=1,
        chat_limit=20,
        chat_window_seconds=60,
        global_limit=30,
        global_window_seconds=1,
        archive_capability=foundation.capability(),
    )
    result = await poller.tick()
    assert result["state"] == "sent" and poller.sent == 1 and poller.deferred == 0
    assert len(sent) == 1 and sent[0][0].endswith("/sendMessage")
    assert card["outcome_text"] in sent[0][1]["text"]
    # The settled card goes out with Configure alone.
    assert sent[0][1]["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "⚙️ Configure", "callback_data": f"v1:cfg:{intent}"}]
        ]
    }
    assert len(calls) == 2 and await poller.tick() is None
    async with obs.uow(archive_engine, origin) as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT state,attempts,external_message_ref FROM channel_outbox WHERE id=:id"
                ),
                {"id": oid},
            )
        ).one()
        assert tuple(row) == ("superseded", 1, "987")


async def test_claim_then_terminal_late_no_send_finishes_on_next_rerender(
    archive_engine, origin
):
    from src import worker
    from src.channels.telegram_transport import TelegramTransport
    from src.services.target import outbox

    prepared, calls, intent, binding, oid = await approved_archive_card(
        archive_engine, origin
    )
    sent = []
    fetched = []

    async def late_fetch(media):
        fetched.append(media["intent_id"])
        if len(fetched) == 1:
            await post_archive_card(archive_engine, origin, intent)
        return await worker._card_media_fetch(
            archive_engine, {}, fetch_selected=prepared.fetch_selected
        )(media)

    def handler(request):
        sent.append(request.url.path)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 988}})

    transport = TelegramTransport(
        "synthetic-token",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        media_fetch=late_fetch,
    )

    def factory():
        return obs.uow(archive_engine, origin)

    poller = outbox.OutboxPoller(
        factory,
        binding_id=binding,
        transport=transport.for_chat("synthetic-chat"),
        clock=lambda: datetime.now(timezone.utc),
        interval_seconds=1,
        chat_limit=20,
        chat_window_seconds=60,
        global_limit=30,
        global_window_seconds=1,
        archive_capability=foundation.capability(),
    )
    first = await poller.tick()
    assert first["state"] == "media_deferred" and sent == [] and fetched == [intent]
    async with obs.uow(archive_engine, origin) as conn:
        assert tuple(
            (
                await conn.execute(
                    text(
                        "SELECT state,attempts,last_failure_class FROM channel_outbox WHERE id=:id"
                    ),
                    {"id": oid},
                )
            ).one()
        ) == ("pending", 0, None)
    second = await poller.tick()
    assert (
        second["state"] == "sent"
        and fetched == [intent]
        and len(sent) == 1
        and sent[0].endswith("/sendMessage")
    )
    assert poller.sent == 1 and poller.deferred == 1 and len(calls) == 2


async def _selected_discovery(world_db, limit=2):
    """Use the worker-only cross-tenant door, regardless of seed actor role."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from tests.scripts.conftest import as_user, async_url

    engine = create_async_engine(async_url(as_user(world_db, "svc_worker")))
    try:
        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT current_user")) == "svc_worker"
            return {
                str(row[0])
                for row in (
                    await conn.execute(
                        text("SELECT intent_id FROM fn_selected_preparation_due(:lim)"),
                        {"lim": limit},
                    )
                ).all()
            }
    finally:
        await engine.dispose()


def _selected_discovery_chain(sync_conn):
    from tests.scripts.conftest import seed_workspace_chain

    chain = seed_workspace_chain(sync_conn, "selected-discovery-" + uuid.uuid4().hex)
    with sync_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO oauth_credentials(workspace_id,ig_account_id,provider,encrypted_payload) "
            "VALUES(%s,%s,'ig_login','test-ciphertext') RETURNING id",
            (chain["ws"], chain["iga"]),
        )
        chain["credential"] = cur.fetchone()[0]
    sync_conn.commit()
    return chain


async def test_selected_discovery_drive_heads_cannot_fill_archive_batch(
    archive_engine, origin, world_db, sync_conn
):
    from tests.scripts.conftest import seed_workspace_chain

    heads = [
        seed_workspace_chain(sync_conn, "drive-head-" + uuid.uuid4().hex)
        for _ in range(3)
    ]
    src, item, intent = await selected(archive_engine, origin)
    due = await _selected_discovery(world_db)
    assert intent in due
    assert not due.intersection(str(h["intent"]) for h in heads)
    assert len(due) <= 2


async def test_selected_discovery_ready_card_heads_cannot_fill_archive_batch(
    archive_engine, origin, world_db, sync_conn
):
    from src.services.target import outbox

    heads = []
    for _ in range(3):
        chain = _selected_discovery_chain(sync_conn)
        src, item, intent = await selected(archive_engine, chain)
        prepared, calls = service(archive_engine, chain)
        await prepared.selected_prepare(
            engine=archive_engine, workspace_id=str(chain["ws"]), intent_id=intent
        )
        async with obs.uow(archive_engine, chain) as conn:
            await conn.execute(
                text("UPDATE post_intents SET state='prompt_pending' WHERE id=:id"),
                {"id": intent},
            )
            await conn.execute(
                text("UPDATE post_intents SET state='awaiting_approval' WHERE id=:id"),
                {"id": intent},
            )
            binding = str(
                await conn.scalar(
                    text(
                        "INSERT INTO channel_bindings(workspace_id,channel,external_ref) VALUES(:ws,'telegram_group',:ref) RETURNING id"
                    ),
                    {"ws": str(chain["ws"]), "ref": "synthetic-" + uuid.uuid4().hex},
                )
            )
            await outbox.enqueue(
                conn,
                workspace_id=str(chain["ws"]),
                binding_id=binding,
                kind="approval_prompt",
                intent_id=intent,
                payload={"v": 1},
            )
        heads.append(intent)
    src, item, newer = await selected(archive_engine, origin)
    due = await _selected_discovery(world_db)
    assert newer in due and not due.intersection(heads) and len(due) <= 2


@pytest.mark.parametrize(
    "change",
    [
        "absent_provenance",
        "partial_proof",
        "generation",
        "reobserve",
        "identity",
        "fingerprint",
    ],
)
async def test_selected_discovery_partial_or_stale_bound_proof_remains_due(
    archive_engine, origin, world_db, sync_conn, change
):
    src, item, intent = await selected(archive_engine, origin)
    prepared, calls = service(archive_engine, origin)
    await prepared.selected_prepare(
        engine=archive_engine, workspace_id=str(origin["ws"]), intent_id=intent
    )
    assert intent not in await _selected_discovery(world_db, 100)
    if change == "generation":
        await foundation.ensure(archive_engine, origin, foundation.authorization())
    elif change == "reobserve":
        await obs.observe(
            archive_engine, origin, src, obs.bundle(attribution="not_detected")
        )
    else:
        with sync_conn.cursor() as cur:
            if change == "absent_provenance":
                cur.execute(
                    "DELETE FROM instagram_intent_provenance WHERE intent_id=%s",
                    (intent,),
                )
            elif change == "partial_proof":
                cur.execute(
                    "UPDATE instagram_intent_provenance SET validation_fingerprint=NULL WHERE intent_id=%s",
                    (intent,),
                )
            elif change == "identity":
                cur.execute(
                    "UPDATE instagram_media_assets SET asset_media_id='456' WHERE workspace_id=%s AND media_item_id=%s",
                    (origin["ws"], item.media_item_id),
                )
            else:
                cur.execute(
                    "UPDATE instagram_media_assets SET validation_fingerprint='ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff' WHERE workspace_id=%s AND media_item_id=%s",
                    (origin["ws"], item.media_item_id),
                )
        sync_conn.commit()
    assert intent in await _selected_discovery(world_db, 100)


async def test_selected_discovery_epoch_only_renewal_does_not_repeat_byte_work(
    archive_engine, origin, world_db
):
    src, item, intent = await selected(archive_engine, origin)
    prepared, calls = service(archive_engine, origin)
    await prepared.selected_prepare(
        engine=archive_engine, workspace_id=str(origin["ws"]), intent_id=intent
    )
    before = await proof(archive_engine, origin, intent)
    async with obs.uow(archive_engine, origin) as conn:
        await conn.execute(
            text(
                "UPDATE media_sources SET source_use_epoch=source_use_epoch+1 WHERE id=:sid"
            ),
            {"sid": src["source_id"]},
        )
    assert intent not in await _selected_discovery(world_db, 100)
    assert (await proof(archive_engine, origin, intent))["capture_epoch"] == before[
        "capture_epoch"
    ]


async def test_selected_discovery_owner_has_only_required_archive_read_grants(
    archive_engine, origin
):
    async with obs.uow(archive_engine, origin) as conn:
        for table, column in (
            ("instagram_intent_provenance", "capture_observation_revision"),
            ("instagram_media_assets", "validation_fingerprint"),
        ):
            assert await conn.scalar(
                text(
                    "SELECT has_column_privilege('svc_maintenance',:table,:column,'SELECT')"
                ),
                {"table": table, "column": column},
            )
            assert not await conn.scalar(
                text(
                    "SELECT has_table_privilege('svc_maintenance',:table,'INSERT') OR has_table_privilege('svc_maintenance',:table,'UPDATE')"
                ),
                {"table": table},
            )
        # `archive lifecycle cleanup`: the redaction door, which svc_maintenance owns, deletes the intent
        # provenance of fenced generations; the assets stay read-only.
        delete = "SELECT has_table_privilege('svc_maintenance',:table,'DELETE')"
        assert await conn.scalar(text(delete), {"table": "instagram_intent_provenance"})
        assert not await conn.scalar(text(delete), {"table": "instagram_media_assets"})
