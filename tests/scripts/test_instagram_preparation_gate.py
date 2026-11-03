"""Selected-only preparation through actual tenant roles, real local JPEG proof."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import BytesIO
import hashlib
import json
import uuid

import httpx
import pytest
from PIL import Image
from sqlalchemy import text

from src.services.target import (
    egress,
    media_adapters,
    media_validation,
    refusal_evidence,
    source_policy,
)
from src.services.target import source_read_admission as admission
from src.services.target.instagram_past_posts_adapter import (
    PastPostsAccess,
    InstagramPastPostsAdapter,
)
from src.services.target.past_posts_retry import ONCE, Retry
from src.services.target.unit_of_work import in_transaction
from tests.scripts import test_candidate_selection_gate as selection
from tests.scripts import test_instagram_observations_gate as obs
from tests.scripts import test_instagram_sources_gate as foundation

past_posts_engine = foundation.past_posts_engine
origin = foundation.origin
pytestmark = pytest.mark.integration


def jpeg(color="red"):
    output = BytesIO()
    Image.new("RGB", (64, 128), color).save(output, format="JPEG")
    return output.getvalue()


def png(color="red"):
    output = BytesIO()
    Image.new("RGB", (64, 128), color).save(output, format="PNG")
    return output.getvalue()


async def selected(engine, chain, *, carousel=False):
    async with obs.uow(engine, chain) as conn:
        await conn.execute(
            text("UPDATE ig_accounts SET provider_account_ref=:ref WHERE id=:id"),
            {
                "id": str(chain["iga"]),
                "ref": str(int(uuid.UUID(str(chain["iga"])).int % 10**16) + 10**16),
            },
        )
    src, item = await selection.metadata(engine, chain, carousel=carousel)
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
    retry=ONCE,
):
    from src.services.target.instagram_preparation import (
        PastPostsPreparationPorts,
        SelectedPreparation,
    )

    calls = []
    data = jpeg() if content is None else content

    async def prepare(capture, config):
        assert not in_transaction()
        if before:
            await before()

        async def access(source_id, *, workspace_id):
            return PastPostsAccess(capture.account_ref, "synthetic-token", capture)

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

        return InstagramPastPostsAdapter(
            access_provider=access,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            graph_version="v26.0",
            policy=egress.EgressPolicy().without(enforce_private_address_block=False),
            admission_provider=admission.admission_provider(
                engine, capability=foundation.capability()
            ),
        )

    return SelectedPreparation(
        PastPostsPreparationPorts(
            foundation.capability(), prepare, obs.keys(), retry=retry
        )
    ), calls


#: Tries again after 30 s, up to three tries.
RETRY = Retry(attempts=3, base_seconds=30, cap_seconds=900)


async def next_try(engine, chain, intent):
    """The slide's capture without its next try, that next try, and the
    database's clock now."""
    capture = dict((await proof(engine, chain, intent))["validation_capture"])
    async with obs.uow(engine, chain) as conn:
        now = await conn.scalar(text("SELECT clock_timestamp()"))
    return capture, datetime.fromisoformat(capture.pop("next_attempt_at")), now


async def test_selected_bytes_bind_before_dispatch_and_real_proof_is_strict(
    past_posts_engine, origin, monkeypatch
):
    src, item, intent = await selected(past_posts_engine, origin)

    validations = []
    validate = media_validation.validate_media

    async def measured(value, **kwargs):
        validations.append(value.data)
        return await validate(value, **kwargs)

    monkeypatch.setattr(media_validation, "validate_media", measured)

    async def verify_bound(request):
        assert await proof(past_posts_engine, origin, intent) is not None

    prepared, calls = service(past_posts_engine, origin, returned=verify_bound)
    result = await prepared.fetch_selected(
        engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
    )
    assert result.content == jpeg() and result.validation.state == "supported"
    assert result.validation.selected_asset_id == "123"
    assert (result.counters.graph_attempts, result.counters.cdn_attempts) == (1, 1)
    row = await proof(past_posts_engine, origin, intent)
    assert (
        row["selected_asset_identity"] == "123"
        and row["validation_fingerprint"] == result.validation.fingerprint
    )
    async with obs.uow(past_posts_engine, origin) as conn:
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
        engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
    )
    assert len(calls) == 2, (
        "coherent preparation short-circuits; card reads separately verify served bytes"
    )
    again = await prepared.fetch_selected(
        engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
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

    async with obs.uow(past_posts_engine, origin) as conn:
        conn.info["past_posts_capability"] = foundation.capability()
        card = await prompts.rerender_prompt(conn, intent_id=intent)
    assert card["media"]["intent_id"] == intent
    data, _, mime = await worker._card_media_fetch(
        past_posts_engine, {}, fetch_selected=prepared.fetch_selected
    )(card["media"])
    assert data == jpeg() and mime == "image/jpeg" and len(calls) == 6
    assert validations == [jpeg()] * 3, "each returned byte stream is validated once"


#: The turn's counts for the fetch of one media item that needs no profile or
#: children read: one Graph answer and one download.
ONE_READ = {
    "graph_attempts": 1,
    "observed_parents": 0,
    "source_fence_drop": 0,
    "listed_parents": 0,
    "cdn_attempts": 1,
}


async def test_a_fetch_records_what_it_read_beside_the_validation(
    past_posts_engine, origin
):
    """The fetch that returned the bytes lands under `validation_capture ->
    'fetch'` beside the validation's state, with the turn's counters: each answer's
    status and the media types, and the file's status, type, size, sha256 and host
    as served (its link carries no expiry). A fetch is not a refusal, so none
    rides with it."""
    content = jpeg()
    _, _, intent = await selected(past_posts_engine, origin)
    prepared, _ = service(past_posts_engine, origin, content=content)
    result = await prepared.fetch_selected(
        engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
    )
    assert result.content == content and result.validation.state == "supported"
    capture = (await proof(past_posts_engine, origin, intent))["validation_capture"]
    assert capture["state"] == "supported" and "refusal" not in capture
    assert capture["fetch"] == {
        "v": 1,
        "kind": "fetched",
        "post": {"http_status": 200, "media_type": "IMAGE"},
        "children": None,
        "part": {"media_type": "IMAGE"},
        "file": {
            "http_status": 200,
            "content_type": "image/jpeg",
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "host": "scontent-test.cdninstagram.com",
            "expiry": None,
        },
        "link_statuses": [],
        "counters": ONE_READ,
    }


async def test_unsupported_bytes_record_the_fetch_that_got_them(
    past_posts_engine, origin
):
    """Bytes that fail a local check still land the fetch's evidence beside the
    check's refusal: what the CDN served, by size and sha256, is the first thing
    to read when its bytes are refused."""
    content = png()
    _, _, intent = await selected(past_posts_engine, origin)
    prepared, _ = service(past_posts_engine, origin, content=content)
    with pytest.raises(source_policy.MediaAdmissionPending) as err:
        await prepared.selected_prepare(
            engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
        )
    assert err.value.reason == "unsupported"
    capture = (await proof(past_posts_engine, origin, intent))["validation_capture"]
    assert capture["state"] == "unsupported"
    assert capture["refusal"]["check"] == "image_format"
    fetched = capture["fetch"]
    assert fetched["kind"] == "fetched"
    assert fetched["file"]["bytes"] == len(content)
    assert fetched["file"]["sha256"] == hashlib.sha256(content).hexdigest()
    assert fetched["counters"] == ONE_READ


async def test_second_slide_prepares_its_own_identity_and_bytes(
    past_posts_engine, origin
):
    from src.services.target.instagram_catalog import PastPostsAsset

    src, _, _ = await selected(past_posts_engine, origin)
    await obs.observe(
        past_posts_engine,
        origin,
        src,
        replace(
            obs.bundle(attribution="not_detected"),
            assets=(
                PastPostsAsset(selected_asset_id="123", media_kind="image", position=1),
                PastPostsAsset(selected_asset_id="456", media_kind="image", position=2),
            ),
            slide_count=2,
        ),
    )
    async with obs.uow(past_posts_engine, origin) as conn:
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
    prepared, calls = service(past_posts_engine, origin, changed_child="456")
    result = await prepared.fetch_selected(
        engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
    )
    assert result.validation.selected_asset_id == "456"
    assert result.content == jpeg()
    assert calls == ["graph.instagram.com"] * 2 + ["scontent-test.cdninstagram.com"]
    row = await proof(past_posts_engine, origin, intent)
    assert row["selected_asset_identity"] == "456"
    assert row["validation_state"] == "supported"


async def test_removed_slide_cannot_replace_bound_card(past_posts_engine, origin):
    src, item, intent = await selected(past_posts_engine, origin)
    prepared, calls = service(past_posts_engine, origin)
    await prepared.selected_prepare(
        engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
    )
    from src.services.target.instagram_catalog import PastPostsAsset

    await obs.observe(
        past_posts_engine,
        origin,
        src,
        replace(
            obs.bundle(attribution="not_detected"),
            assets=(PastPostsAsset(selected_asset_id="456", media_kind="image"),),
            slide_count=1,
        ),
    )
    with pytest.raises(source_policy.MediaAdmissionPending) as err:
        await prepared.fetch_selected(
            engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
        )
    assert err.value.reason == "asset_unavailable" and len(calls) == 2
    async with obs.uow(past_posts_engine, origin) as conn:
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
    past_posts_engine, origin, monkeypatch, when
):
    src, item, intent = await selected(past_posts_engine, origin)

    async def off():
        async with obs.uow(past_posts_engine, origin) as conn:
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
        past_posts_engine,
        origin,
        before=off if when == "before_dispatch" else None,
        returned=returned if when == "after_graph" else None,
    )
    with pytest.raises(
        (admission.SourceReadRefused, media_adapters.MediaSourceFenceDrop)
    ):
        await prepared.fetch_selected(
            engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
        )
    assert len(calls) == {"before_dispatch": 0, "after_graph": 1, "before_cas": 2}[when]
    row = await proof(past_posts_engine, origin, intent)
    assert (
        row["validation_fingerprint"] is None and row["validation_state"] == "pending"
    )


async def test_observation_race_refuses_cas_and_keeps_historical_selection(
    past_posts_engine, origin, monkeypatch
):
    src, item, intent = await selected(past_posts_engine, origin)
    validate = media_validation.validate_media

    async def race(value, **kwargs):
        result = await validate(value, **kwargs)
        await obs.observe(
            past_posts_engine,
            origin,
            src,
            obs.bundle(attribution="not_detected", like_count=2),
        )
        return result

    monkeypatch.setattr(media_validation, "validate_media", race)
    prepared, calls = service(past_posts_engine, origin)
    with pytest.raises(source_policy.MediaAdmissionPending) as err:
        await prepared.fetch_selected(
            engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
        )
    assert err.value.reason == "past_posts_revalidation_required"
    row = await proof(past_posts_engine, origin, intent)
    assert (
        row["selection_observation_revision"] == 1
        and row["validation_fingerprint"] is None
    )


async def test_unsupported_selected_bytes_become_coherent_candidate_exclusion(
    past_posts_engine, origin
):
    src, item, intent = await selected(past_posts_engine, origin)
    prepared, calls = service(past_posts_engine, origin, content=b"corrupt JPEG")
    with pytest.raises(source_policy.MediaAdmissionPending) as err:
        await prepared.selected_prepare(
            engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
        )
    assert err.value.reason == "unsupported"
    row = await proof(past_posts_engine, origin, intent)
    assert (
        row["validation_state"] == "unsupported"
        and row["validation_capture"]["state"] == "unsupported"
    )
    async with obs.uow(past_posts_engine, origin) as conn:
        assert (await selection.verdict(conn, origin, src, intent=intent))[
            "reason"
        ] == "unsupported"


async def test_new_grant_after_response_fences_existing_selected_capture(
    past_posts_engine, origin
):
    src, item, intent = await selected(past_posts_engine, origin)

    async def renew(request):
        if request.url.host == "graph.instagram.com":
            await foundation.ensure(
                past_posts_engine, origin, foundation.authorization()
            )

    prepared, calls = service(past_posts_engine, origin, returned=renew)
    with pytest.raises(media_adapters.MediaSourceFenceDrop) as err:
        await prepared.fetch_selected(
            engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
        )
    assert err.value.reason == "source_generation_changed" and len(calls) == 1
    row = await proof(past_posts_engine, origin, intent)
    assert (
        row["validation_fingerprint"] is None
        and str(row["selection_generation_id"]) == src["generation_id"]
    )


async def test_staged_capture_cannot_change_proof_even_when_same_asset_bytes_change(
    past_posts_engine, origin
):
    import uuid
    from src.services.target import instagram_artifacts

    src, item, intent = await selected(past_posts_engine, origin)
    prepared, calls = service(past_posts_engine, origin)
    original = await prepared.fetch_selected(
        engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
    )
    async with obs.uow(past_posts_engine, origin) as conn:
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
    altered, _ = service(past_posts_engine, origin, content=jpeg("blue"))
    with pytest.raises(source_policy.MediaAdmissionPending) as err:
        await altered.fetch_selected(
            engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
        )
    assert err.value.reason == "past_posts_revalidation_required"
    row = await proof(past_posts_engine, origin, intent)
    assert (
        row["validation_fingerprint"] == original.validation.fingerprint
        and str(row["transit_artifact_ref"]) == ref
    )


async def test_transient_selected_failure_preserves_metadata_and_permits_retry(
    past_posts_engine, origin
):
    from src.services.target.instagram_preparation import (
        PastPostsPreparationPorts,
        SelectedPreparation,
    )

    src, item, intent = await selected(past_posts_engine, origin)

    class TransientAdapter:
        async def fetch_bytes(self, *args, **kwargs):
            kwargs["on_counters"](media_adapters.MediaTurnCounters(graph_attempts=1))
            raise media_adapters.MediaRetryableError("synthetic")

    async def prepare(capture, config):
        return TransientAdapter()

    transient = SelectedPreparation(
        PastPostsPreparationPorts(foundation.capability(), prepare, obs.keys())
    )
    with pytest.raises(media_adapters.MediaRetryableError):
        await transient.selected_prepare(
            engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
        )
    row = await proof(past_posts_engine, origin, intent)
    assert row["validation_capture"]["reason"] == "download_pending"
    async with obs.uow(past_posts_engine, origin) as conn:
        assert (await selection.verdict(conn, origin, src, intent=intent))["suitable"]
    prepared, calls = service(past_posts_engine, origin)
    await prepared.selected_prepare(
        engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
    )
    assert (
        len(calls) == 2
        and (await proof(past_posts_engine, origin, intent))["validation_state"]
        == "supported"
    )


#: The turn's counts as an adapter reports them before a refusal. Each is
#: distinct, so a count recorded under another name fails.
TURN = {
    "graph_attempts": 2,
    "observed_parents": 1,
    "source_fence_drop": 3,
    "listed_parents": 4,
    "cdn_attempts": 5,
}


def refusing(error, *, given=None):
    """A preparation whose adapter reports `TURN`, then raises `error`; the
    keyword arguments each fetch receives are appended to `given`."""
    from src.services.target.instagram_preparation import (
        PastPostsPreparationPorts,
        SelectedPreparation,
    )

    class RefusingAdapter:
        async def fetch_bytes(self, *args, **kwargs):
            if given is not None:
                given.append(kwargs)
            kwargs["on_counters"](media_adapters.MediaTurnCounters(**TURN))
            raise error

    async def prepare(capture, config):
        return RefusingAdapter()

    return SelectedPreparation(
        PastPostsPreparationPorts(foundation.capability(), prepare, obs.keys())
    )


def serving(content):
    """A preparation whose adapter reports `TURN`, then returns `content` as the
    CDN's JPEG-labelled bytes."""
    from src.services.target.instagram_preparation import (
        PastPostsPreparationPorts,
        SelectedPreparation,
    )

    class ServingAdapter:
        async def fetch_bytes(self, *args, **kwargs):
            kwargs["on_counters"](media_adapters.MediaTurnCounters(**TURN))
            return content, "Instagram 123.jpg", "image/jpeg"

    async def prepare(capture, config):
        return ServingAdapter()

    return SelectedPreparation(
        PastPostsPreparationPorts(foundation.capability(), prepare, obs.keys())
    )


def no_media_link():
    """A terminal refusal carrying the evidence of a part with no link."""
    return refusal_evidence.attach(
        media_adapters.MediaTerminalError("synthetic"),
        refusal_evidence.evidence("no_media_link", media_type="IMAGE", has_link=False),
    )


class SyntheticRefusal(media_adapters.MediaTerminalError):
    """A terminal refusal raised with no evidence attached."""


async def test_terminal_refusal_records_its_evidence_with_the_turn_counters(
    past_posts_engine, origin
):
    """A terminal fetch refusal's evidence lands, with the turn's counters, under
    the asset's `validation_capture -> 'refusal'` beside its usual keys and on
    the pending it raises. The reason stays `asset_unavailable`, so the story
    reads as unavailable exactly as before, and no proof is granted."""
    src, item, intent = await selected(past_posts_engine, origin)
    prepared = refusing(no_media_link())
    with pytest.raises(source_policy.MediaAdmissionPending) as err:
        await prepared.selected_prepare(
            engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
        )
    refusal = {
        "v": 1,
        "kind": "no_media_link",
        "media_type": "IMAGE",
        "has_link": False,
        "counters": TURN,
    }
    assert err.value.reason == "asset_unavailable" and err.value.evidence == refusal
    row = await proof(past_posts_engine, origin, intent)
    assert row["validation_capture"] == {
        "generation_id": src["generation_id"],
        "observation_revision": 1,
        "selected_asset_id": "123",
        "state": "validation_pending",
        "reason": "asset_unavailable",
        "refusal": refusal,
    }
    assert (
        row["validation_state"] == "pending" and row["validation_fingerprint"] is None
    )
    async with obs.uow(past_posts_engine, origin) as conn:
        verdict = await selection.verdict(conn, origin, src, intent=intent)
    assert verdict["reason"] == "asset_unavailable" and not verdict["suitable"]


@pytest.mark.parametrize(
    "error_type", [media_adapters.MediaTerminalError, SyntheticRefusal]
)
async def test_terminal_refusal_without_evidence_records_its_error_class(
    past_posts_engine, origin, error_type
):
    """A terminal refusal that carries no evidence is recorded as `unrecorded`
    naming the error's own class, so a refusal without evidence is still told
    from no refusal; the turn's counters ride with it."""
    _, _, intent = await selected(past_posts_engine, origin)
    prepared = refusing(error_type("synthetic"))
    with pytest.raises(source_policy.MediaAdmissionPending) as err:
        await prepared.selected_prepare(
            engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
        )
    refusal = {
        "v": 1,
        "kind": "unrecorded",
        "error": error_type.__name__,
        "counters": TURN,
    }
    assert err.value.evidence == refusal
    capture = (await proof(past_posts_engine, origin, intent))["validation_capture"]
    assert capture["reason"] == "asset_unavailable" and capture["refusal"] == refusal


async def test_retryable_refusal_records_its_evidence_under_download_pending(
    past_posts_engine, origin
):
    """A retryable fetch refusal's evidence lands in the asset's
    `validation_capture` under `download_pending`, and the original error is
    raised again. `download_pending` does not latch the story."""
    src, item, intent = await selected(past_posts_engine, origin)
    refused = refusal_evidence.attach(
        media_adapters.MediaRetryableError("synthetic"),
        refusal_evidence.evidence(
            "cdn_error",
            statuses=[503],
            host="scontent-test.cdninstagram.com",
            expiry="1900000000",
        ),
    )
    prepared = refusing(refused)
    with pytest.raises(media_adapters.MediaRetryableError) as err:
        await prepared.selected_prepare(
            engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
        )
    assert err.value is refused
    row = await proof(past_posts_engine, origin, intent)
    assert row["validation_capture"] == {
        "generation_id": src["generation_id"],
        "observation_revision": 1,
        "selected_asset_id": "123",
        "state": "validation_pending",
        "reason": "download_pending",
        "refusal": {
            "v": 1,
            "kind": "cdn_error",
            "statuses": [503],
            "host": "scontent-test.cdninstagram.com",
            "expiry": "1900000000",
            "counters": TURN,
        },
    }
    async with obs.uow(past_posts_engine, origin) as conn:
        assert (await selection.verdict(conn, origin, src, intent=intent))["suitable"]


async def test_unsupported_bytes_record_the_failed_check_with_what_it_measured(
    past_posts_engine, origin
):
    """Bytes that fail a local check land that check's refusal evidence (the
    state, the check, the values it measured, the byte count, the matrix
    version and the turn's counters) under the asset's `validation_capture ->
    'refusal'` beside its usual keys, and on the pending it raises. The latch
    is unchanged: the asset reads `unsupported` and the candidate stays
    excluded as `unsupported`."""
    src, _, intent = await selected(past_posts_engine, origin)
    content = png()
    prepared = serving(content)
    with pytest.raises(source_policy.MediaAdmissionPending) as err:
        await prepared.selected_prepare(
            engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
        )
    refusal = {
        "v": 1,
        "kind": "local_validation",
        "state": "unsupported",
        "check": "image_format",
        "measured": {"format": "PNG"},
        "bytes": len(content),
        "matrix_version": media_validation.MATRIX_VERSION,
        "counters": TURN,
    }
    assert err.value.reason == "unsupported" and err.value.evidence == refusal
    row = await proof(past_posts_engine, origin, intent)
    capture = row["validation_capture"]
    assert row["validation_state"] == "unsupported"
    assert capture["state"] == "unsupported" and capture["reason"] == "image_format"
    assert capture["refusal"] == refusal
    async with obs.uow(past_posts_engine, origin) as conn:
        verdict = await selection.verdict(conn, origin, src, intent=intent)
    assert verdict["reason"] == "unsupported" and not verdict["suitable"]


async def test_supported_bytes_record_each_check_they_passed_with_what_it_measured(
    past_posts_engine, origin
):
    """Bytes that pass land what they passed, in a refusal's shape (the state,
    each check with the values it measured and the bound it held, the byte
    count, the matrix version and the turn's counters), under the asset's
    `validation_capture -> 'passed'`, and no refusal."""
    _, _, intent = await selected(past_posts_engine, origin)
    content = jpeg()
    await serving(content).selected_prepare(
        engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
    )
    capture = (await proof(past_posts_engine, origin, intent))["validation_capture"]
    cap = media_validation.SUPPORTED_MEDIA_MATRIX["byte_caps"]["image"]
    assert capture["state"] == "supported" and "refusal" not in capture
    assert capture["passed"] == {
        "v": 1,
        "kind": "local_validation",
        "state": "supported",
        "checks": [
            {"check": "byte_limit", "measured": {"bytes": len(content), "cap": cap}}
        ],
        "bytes": len(content),
        "matrix_version": media_validation.MATRIX_VERSION,
        "counters": TURN,
    }


async def test_slide_gone_refusal_carries_its_evidence_and_the_indexed_slide_count(
    past_posts_engine, origin
):
    """A slide judged gone retires its asset, which deletes the row that would
    hold the refusal, so the evidence rides on the pending alone: the adapter's
    facts, the turn's counters and the slide count the index recorded, to set
    against the children the read returned."""
    from src.services.target.instagram_catalog import PastPostsAsset

    src, item, intent = await selected(past_posts_engine, origin)
    await obs.observe(
        past_posts_engine,
        origin,
        src,
        replace(
            obs.bundle(attribution="not_detected"),
            assets=(
                PastPostsAsset(selected_asset_id="123", media_kind="image", position=1),
                PastPostsAsset(selected_asset_id="456", media_kind="image", position=2),
            ),
            slide_count=2,
        ),
    )
    prepared = refusing(
        refusal_evidence.attach(
            media_adapters.MediaSelectedAssetGone(),
            refusal_evidence.evidence("slide_gone", children=1, complete=True),
        )
    )
    with pytest.raises(source_policy.MediaAdmissionPending) as err:
        await prepared.fetch_selected(
            engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
        )
    assert err.value.reason == "asset_unavailable"
    assert err.value.evidence == {
        "v": 1,
        "kind": "slide_gone",
        "children": 1,
        "complete": True,
        "counters": TURN,
        "slide_count": 2,
    }
    async with obs.uow(past_posts_engine, origin) as conn:
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
    assert (await proof(past_posts_engine, origin, intent))[
        "validation_capture"
    ] is None


async def test_reobservation_clears_a_recorded_refusal(past_posts_engine, origin):
    """A refusal belongs to the observation it refused: the next observation of
    the post clears it with the rest of `validation_capture`, and the story is
    eligible again."""
    src, item, intent = await selected(past_posts_engine, origin)
    prepared = refusing(no_media_link())
    with pytest.raises(source_policy.MediaAdmissionPending):
        await prepared.selected_prepare(
            engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
        )
    assert (
        "refusal"
        in (await proof(past_posts_engine, origin, intent))["validation_capture"]
    )
    async with obs.uow(past_posts_engine, origin) as conn:
        assert (await selection.verdict(conn, origin, src, intent=intent))[
            "reason"
        ] == "asset_unavailable"
    await obs.observe(
        past_posts_engine, origin, src, obs.bundle(attribution="not_detected")
    )
    assert (await proof(past_posts_engine, origin, intent))[
        "validation_capture"
    ] is None
    async with obs.uow(past_posts_engine, origin) as conn:
        assert (await selection.verdict(conn, origin, src, intent=intent))["suitable"]


async def test_cancel_requested_before_validation_cas_refuses_selected_bytes(
    past_posts_engine, origin, monkeypatch
):
    src, item, intent = await selected(past_posts_engine, origin)
    validate = media_validation.validate_media

    async def cancel(value, **kwargs):
        result = await validate(value, **kwargs)
        async with obs.uow(past_posts_engine, origin) as conn:
            await conn.execute(
                text("UPDATE post_intents SET cancel_requested=true WHERE id=:id"),
                {"id": intent},
            )
        return result

    monkeypatch.setattr(media_validation, "validate_media", cancel)
    prepared, calls = service(past_posts_engine, origin)
    with pytest.raises(source_policy.MediaAdmissionPending):
        await prepared.fetch_selected(
            engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
        )
    assert (await proof(past_posts_engine, origin, intent))[
        "validation_fingerprint"
    ] is None


async def test_remote_changed_first_asset_is_coherent_unavailable_without_cdn(
    past_posts_engine, origin
):
    from src.services.target.instagram_catalog import PastPostsAsset

    src, item, intent = await selected(past_posts_engine, origin)
    carousel = replace(
        obs.bundle(attribution="not_detected"),
        assets=(
            PastPostsAsset(selected_asset_id="123", media_kind="image", position=1),
            PastPostsAsset(selected_asset_id="456", media_kind="image", position=2),
        ),
        slide_count=2,
    )
    await obs.observe(past_posts_engine, origin, src, carousel)

    async def keep_lock():
        async with obs.uow(past_posts_engine, origin) as conn:
            await conn.execute(
                text(
                    "INSERT INTO post_locks(workspace_id,media_item_id,kind) VALUES(:ws,:item,'hold')"
                ),
                {"ws": str(origin["ws"]), "item": item.media_item_id},
            )

    prepared, calls = service(
        past_posts_engine, origin, changed_child="456", before=keep_lock
    )
    with pytest.raises(source_policy.MediaAdmissionPending) as error:
        await prepared.fetch_selected(
            engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
        )
    assert error.value.reason == "asset_unavailable"
    # Parent and children, then both again: the slide is gone only when a
    # second read agrees with the first.
    assert calls == ["graph.instagram.com"] * 4
    async with obs.uow(past_posts_engine, origin) as conn:
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
    await obs.observe(past_posts_engine, origin, src, carousel)
    async with obs.uow(past_posts_engine, origin) as conn:
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
    past_posts_engine, origin
):
    """A truncated children read cannot prove the selected slide left: the story
    waits for its next try with the read's `slide_changed` evidence, and nothing
    is retired."""
    src, item, intent = await selected(past_posts_engine, origin)
    prepared, calls = service(
        past_posts_engine, origin, changed_child="456", truncated=True, retry=RETRY
    )
    with pytest.raises(source_policy.MediaAdmissionPending) as error:
        await prepared.fetch_selected(
            engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
        )
    assert error.value.reason == "past_posts_revalidation_required"
    assert calls == ["graph.instagram.com"] * 2
    capture, next_at, now = await next_try(past_posts_engine, origin, intent)
    assert (capture["reason"], capture["attempt"]) == ("download_pending", 1)
    assert {
        key: capture["refusal"][key] for key in ("kind", "children", "complete")
    } == {"kind": "slide_changed", "children": 1, "complete": False}
    assert now < next_at <= now + timedelta(seconds=RETRY.base_seconds)
    async with obs.uow(past_posts_engine, origin) as conn:
        assert (await selection.verdict(conn, origin, src, intent=intent))["suitable"]
    async with obs.uow(past_posts_engine, origin) as conn:
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


@pytest.mark.parametrize("carousel", [False, True], ids=["single", "carousel"])
async def test_the_fetch_is_given_the_slide_count_the_index_recorded(
    past_posts_engine, origin, carousel
):
    """A missing slide is judged against the index's own count, so the
    preparation hands the fetch the selected post's recorded slide count, and
    none for a single post."""
    src, item, intent = await selected(past_posts_engine, origin, carousel=carousel)
    given = []
    prepared = refusing(media_adapters.MediaRetryableError("synthetic"), given=given)
    with pytest.raises(media_adapters.MediaRetryableError):
        await prepared.selected_prepare(
            engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
        )
    assert [kwargs.get("expected_slide_count", "not passed") for kwargs in given] == [
        2 if carousel else None
    ]


async def test_a_changed_slide_inside_its_wait_is_deferred_without_a_read(
    past_posts_engine, origin
):
    """Before its next try a story waits for revalidation and nothing is read:
    no Graph request, no download."""
    _, _, intent = await selected(past_posts_engine, origin)
    changed, _ = service(
        past_posts_engine, origin, changed_child="456", truncated=True, retry=RETRY
    )
    with pytest.raises(source_policy.MediaAdmissionPending):
        await changed.fetch_selected(
            engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
        )
    prepared, calls = service(past_posts_engine, origin, retry=RETRY)
    with pytest.raises(source_policy.MediaAdmissionPending) as error:
        await prepared.fetch_selected(
            engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
        )
    assert error.value.reason == "past_posts_revalidation_required" and calls == []


async def test_a_changed_slide_parks_with_its_evidence_once_its_tries_are_spent(
    past_posts_engine, origin
):
    """The last try parks the story `asset_unavailable` with the read's
    `slide_changed` evidence; the slide is not retired."""
    src, item, intent = await selected(past_posts_engine, origin)
    twice = Retry(attempts=2, base_seconds=0, cap_seconds=0)
    changed, calls = service(
        past_posts_engine, origin, changed_child="456", truncated=True, retry=twice
    )
    with pytest.raises(source_policy.MediaAdmissionPending) as first:
        await changed.fetch_selected(
            engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
        )
    with pytest.raises(source_policy.MediaAdmissionPending) as last:
        await changed.fetch_selected(
            engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
        )
    assert first.value.reason == "past_posts_revalidation_required"
    assert last.value.reason == "asset_unavailable"
    assert last.value.evidence["kind"] == "slide_changed"
    assert calls == ["graph.instagram.com"] * 4
    capture = (await proof(past_posts_engine, origin, intent))["validation_capture"]
    assert (capture["reason"], capture["attempt"], capture["refusal"]) == (
        "asset_unavailable",
        2,
        last.value.evidence,
    )
    assert "next_attempt_at" not in capture
    async with obs.uow(past_posts_engine, origin) as conn:
        verdict = await selection.verdict(conn, origin, src, intent=intent)
        assert (
            await conn.scalar(
                text("SELECT state FROM media_items WHERE id=:id"),
                {"id": item.media_item_id},
            )
            == "available"
        )
    assert verdict["reason"] == "asset_unavailable" and not verdict["suitable"]


@pytest.mark.parametrize("stale", ["reobserved", "policy", "cancelled"])
async def test_selected_gone_fresh_cas_refuses_stale_or_cancelled_turn(
    past_posts_engine, origin, stale
):
    # A carousel, so the two reads that lack slide 123 prove it gone against
    # the indexed count and the retirement's own check is what refuses.
    src, item, intent = await selected(past_posts_engine, origin, carousel=True)

    async def change_while_fetching():
        if stale == "reobserved":
            await obs.observe(
                past_posts_engine,
                origin,
                src,
                obs.bundle(attribution="not_detected"),
            )
            return
        async with obs.uow(past_posts_engine, origin) as conn:
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
        past_posts_engine, origin, changed_child="456", before=change_while_fetching
    )
    with pytest.raises(source_policy.MediaAdmissionPending) as error:
        await prepared.fetch_selected(
            engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
        )
    assert error.value.reason == "past_posts_revalidation_required"
    assert calls == ["graph.instagram.com"] * 4
    async with obs.uow(past_posts_engine, origin) as conn:
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


async def approved_past_posts_card(engine, chain):
    from src.services.target import auto_approval, intent_ledger, outbox, prompts

    src, item, intent = await selected(engine, chain)
    prepared, calls = service(engine, chain)
    await prepared.selected_prepare(
        engine=engine, workspace_id=str(chain["ws"]), intent_id=intent
    )
    async with obs.uow(engine, chain) as conn:
        conn.info["past_posts_capability"] = foundation.capability()
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


async def post_past_posts_card(engine, chain, intent, *, via="api"):
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


async def test_a_trimmed_video_says_so_on_its_card(past_posts_engine, origin):
    """The card reads a past-posts video's trim from its selected slide's
    validation evidence, through the story's provenance."""
    from src.services.target import prompts

    _, _, intent, _, _ = await approved_past_posts_card(past_posts_engine, origin)
    async with obs.uow(past_posts_engine, origin) as conn:
        changed = await conn.execute(
            text(
                "UPDATE instagram_media_assets a SET validation_capture="
                "a.validation_capture||jsonb_build_object('evidence',"
                "COALESCE(a.validation_capture->'evidence','{}'::jsonb)"
                '||\'{"trimmed_to_seconds":"60"}\'::jsonb)'
                " FROM instagram_intent_provenance p"
                " WHERE p.workspace_id=a.workspace_id AND p.intent_id=CAST(:id AS uuid)"
                " AND a.source_id=p.source_id AND a.media_item_id=p.media_item_id"
                " AND a.asset_media_id=p.selected_asset_identity"
            ),
            {"id": intent},
        )
        assert changed.rowcount == 1
        card = await prompts.rerender_prompt(conn, intent_id=intent)
    assert "✂️ Trimmed to 60 s" in card["text"]


@pytest.mark.parametrize("via", ["api", "dry_run"])
async def test_posted_rule_past_posts_card_sends_text_outcome_without_selected_fetch(
    past_posts_engine, origin, via
):
    from src.channels.telegram_transport import TelegramTransport
    from src.services.target import outbox, prompts

    prepared, calls, intent, binding, oid = await approved_past_posts_card(
        past_posts_engine, origin
    )
    await post_past_posts_card(past_posts_engine, origin, intent, via=via)
    async with obs.uow(past_posts_engine, origin) as conn:
        card = await prompts.rerender_prompt(conn, intent_id=intent)
        assert (
            "media" not in card and "caption" not in card and "reply_markup" not in card
        )
        assert ("Dry run" if via == "dry_run" else "Posted by YOLO") in card[
            "outcome_text"
        ]
    sent = []

    async def no_fetch(media):
        raise AssertionError(
            "terminal past-posts outcome must never fetch selected bytes"
        )

    def handler(request):
        sent.append((request.url.path, json.loads(request.read())))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 987}})

    transport = TelegramTransport(
        "synthetic-token",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        media_fetch=no_fetch,
    )

    def factory():
        return obs.uow(past_posts_engine, origin)

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
        past_posts_capability=foundation.capability(),
    )
    result = await poller.tick()
    assert result["state"] == "sent" and poller.sent == 1 and poller.deferred == 0
    assert len(sent) == 1 and sent[0][0].endswith("/sendMessage")
    assert card["outcome_text"] in sent[0][1]["text"]
    # The settled card leaves bare; the edit that follows brings Configure alone.
    assert "reply_markup" not in sent[0][1]
    edit = await poller.tick()
    assert edit["state"] == "sent" and edit["kind"] == "prompt_supersede"
    assert len(sent) == 2 and sent[1][0].endswith("/editMessageReplyMarkup")
    assert sent[1][1]["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "⚙️ Configure", "callback_data": f"v1:cfg:{intent}"}]
        ]
    }
    assert len(calls) == 2 and await poller.tick() is None
    async with obs.uow(past_posts_engine, origin) as conn:
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
    past_posts_engine, origin
):
    from src import worker
    from src.channels.telegram_transport import TelegramTransport
    from src.services.target import outbox

    prepared, calls, intent, binding, oid = await approved_past_posts_card(
        past_posts_engine, origin
    )
    sent = []
    fetched = []

    async def late_fetch(media):
        fetched.append(media["intent_id"])
        if len(fetched) == 1:
            await post_past_posts_card(past_posts_engine, origin, intent)
        return await worker._card_media_fetch(
            past_posts_engine, {}, fetch_selected=prepared.fetch_selected
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
        return obs.uow(past_posts_engine, origin)

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
        past_posts_capability=foundation.capability(),
    )
    first = await poller.tick()
    assert first["state"] == "media_deferred" and sent == [] and fetched == [intent]
    async with obs.uow(past_posts_engine, origin) as conn:
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


async def test_selected_discovery_drive_heads_cannot_fill_past_posts_batch(
    past_posts_engine, origin, world_db, sync_conn
):
    from tests.scripts.conftest import seed_workspace_chain

    heads = [
        seed_workspace_chain(sync_conn, "drive-head-" + uuid.uuid4().hex)
        for _ in range(3)
    ]
    src, item, intent = await selected(past_posts_engine, origin)
    due = await _selected_discovery(world_db)
    assert intent in due
    assert not due.intersection(str(h["intent"]) for h in heads)
    assert len(due) <= 2


async def test_selected_discovery_ready_card_heads_cannot_fill_past_posts_batch(
    past_posts_engine, origin, world_db, sync_conn
):
    from src.services.target import outbox

    heads = []
    for _ in range(3):
        chain = _selected_discovery_chain(sync_conn)
        src, item, intent = await selected(past_posts_engine, chain)
        prepared, calls = service(past_posts_engine, chain)
        await prepared.selected_prepare(
            engine=past_posts_engine, workspace_id=str(chain["ws"]), intent_id=intent
        )
        async with obs.uow(past_posts_engine, chain) as conn:
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
    src, item, newer = await selected(past_posts_engine, origin)
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
    past_posts_engine, origin, world_db, sync_conn, change
):
    src, item, intent = await selected(past_posts_engine, origin)
    prepared, calls = service(past_posts_engine, origin)
    await prepared.selected_prepare(
        engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
    )
    assert intent not in await _selected_discovery(world_db, 100)
    if change == "generation":
        await foundation.ensure(past_posts_engine, origin, foundation.authorization())
    elif change == "reobserve":
        await obs.observe(
            past_posts_engine, origin, src, obs.bundle(attribution="not_detected")
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
    past_posts_engine, origin, world_db
):
    src, item, intent = await selected(past_posts_engine, origin)
    prepared, calls = service(past_posts_engine, origin)
    await prepared.selected_prepare(
        engine=past_posts_engine, workspace_id=str(origin["ws"]), intent_id=intent
    )
    before = await proof(past_posts_engine, origin, intent)
    async with obs.uow(past_posts_engine, origin) as conn:
        await conn.execute(
            text(
                "UPDATE media_sources SET source_use_epoch=source_use_epoch+1 WHERE id=:sid"
            ),
            {"sid": src["source_id"]},
        )
    assert intent not in await _selected_discovery(world_db, 100)
    assert (await proof(past_posts_engine, origin, intent))["capture_epoch"] == before[
        "capture_epoch"
    ]


async def test_selected_discovery_owner_has_only_required_past_posts_read_grants(
    past_posts_engine, origin
):
    async with obs.uow(past_posts_engine, origin) as conn:
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
        # past-posts lifecycle cleanup: the redaction door, which svc_maintenance owns, deletes the intent
        # provenance of fenced generations; the assets stay read-only.
        delete = "SELECT has_table_privilege('svc_maintenance',:table,'DELETE')"
        assert await conn.scalar(text(delete), {"table": "instagram_intent_provenance"})
        assert not await conn.scalar(text(delete), {"table": "instagram_media_assets"})
