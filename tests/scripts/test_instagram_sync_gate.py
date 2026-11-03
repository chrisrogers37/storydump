"""Past-posts sync atomic landing, retained identities and source/lease fences."""

import importlib.util
import json
import logging

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src.services.target import jobs
from src.services.target.media_adapters import MediaPage, MediaTurnCounters
from tests.scripts import test_source_read_admission_gate as admission
from tests.scripts import test_instagram_sources_gate as foundation
from tests.scripts import test_instagram_observations_gate as obs
from tests.scripts import test_instagram_slides_gate as slides
from tests.scripts.conftest import as_user, async_url

pytestmark = [pytest.mark.integration]
past_posts_engine = foundation.past_posts_engine
origin = foundation.origin


def service():
    assert importlib.util.find_spec("src.services.target.instagram_sync"), (
        "atomic past-posts sync missing"
    )
    from src.services.target import instagram_sync

    return instagram_sync


async def leased(world_db, engine, origin, src):
    capture = await admission.capture(engine, origin, src)
    async with obs.uow(engine, origin) as conn:
        jid = await jobs.enqueue(
            conn,
            kind="sync_media_source",
            workspace_id=str(origin["ws"]),
            serialization_key="src:" + src["source_id"],
            payload={
                "v": 1,
                "source_id": src["source_id"],
                "source_read_capture": capture.payload(),
            },
        )
    worker = create_async_engine(async_url(as_user(world_db, "svc_worker")))
    async with worker.connect() as conn:
        job = await jobs.claim_job(
            conn,
            lane="bulk",
            worker="past-posts-sync-test",
            lease_seconds=60,
            ws_lane_cap=4,
            capability=foundation.capability(),
        )
    assert str(job["id"]) == jid
    return worker, job, capture


class Adapter:
    def __init__(self, page, callback=None):
        self.page, self.callback, self.calls = page, callback, 0

    def initial_checkpoint(self):
        return {"v": 2}

    def normalize_checkpoint(self, checkpoint):
        return checkpoint

    async def list_changes(self, config, checkpoint, *, source_id, workspace_id):
        self.calls += 1
        if self.callback:
            await self.callback()
        return self.page


def page(*observations):
    return MediaPage(
        [],
        {
            "v": 2,
            "upper": 2000000000,
            "until": 1999000000,
            "window_start": 1992224000,
            "boundary": [],
            "seen": len(observations),
            "more": True,
        },
        observations=tuple(observations),
        counters=MediaTurnCounters(
            graph_attempts=1,
            observed_parents=len(observations),
            listed_parents=len(observations),
        ),
    )


async def ports(module, adapter, captured):
    async def prepare(capture, provider):
        assert capture == captured
        assert callable(provider)
        return adapter

    return module.PastPostsSyncPorts(
        capability=foundation.capability(),
        tombstone_keys=obs.keys(),
        prepare_adapter=prepare,
    )


async def state(engine, origin, src):
    async with obs.uow(engine, origin) as conn:
        cp = (
            await conn.execute(
                text("SELECT sync_checkpoint FROM media_sources WHERE id=:sid"),
                dict(sid=src["source_id"]),
            )
        ).scalar_one()
        rows = (
            await conn.execute(
                text(
                    "SELECT i.parent_media_id,i.observation_revision,"
                    "(SELECT a.media_item_id FROM instagram_media_assets a "
                    "WHERE a.workspace_id=i.workspace_id AND a.source_id=i.source_id "
                    "AND a.parent_media_id=i.parent_media_id ORDER BY a.position LIMIT 1) "
                    "AS media_item_id FROM instagram_media_index i "
                    "WHERE i.source_id=:sid ORDER BY i.parent_media_id"
                ),
                dict(sid=src["source_id"]),
            )
        ).all()
        queued = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM jobs WHERE payload->>'source_id'=:sid AND state='ready'"
                ),
                dict(sid=src["source_id"]),
            )
        ).scalar_one()
        return cp, rows, queued


async def test_atomic_bundle_checkpoint_progress_and_continuation(
    world_db, past_posts_engine, origin
):
    m = service()
    src = await admission.ready(past_posts_engine, origin)
    worker, job, c = await leased(world_db, past_posts_engine, origin, src)
    try:
        adapter = Adapter(page(obs.bundle()))
        result = await m.run_sync(worker, job, ports=await ports(m, adapter, c))
        cp, rows, queued = await state(past_posts_engine, origin, src)
        assert result.outcome == "committed" and result.committed_observations == 1
        assert len(rows) == 1 and queued == 1
        assert cp["type"] == "instagram_past_posts_sync" and cp["revision"] == 1
        assert cp["generation_id"] == c.generation_id
        assert cp["progress"]["recent"]["observations"] == 1
        assert cp["summary"]["observed"] == 1 and cp["summary"]["represented"] == 1
        assert cp["summary"]["eligible"] == 0
    finally:
        await worker.dispose()


def fault_at(stage):
    """A landing fault that raises at `stage`."""

    async def fault(at):
        if at == stage:
            raise RuntimeError("injected landing fault")

    return fault


@pytest.mark.parametrize(
    "stage", ["observation", "progress", "checkpoint", "continuation", "before_commit"]
)
async def test_fault_rolls_back_whole_landing(
    world_db, past_posts_engine, origin, stage
):
    m = service()
    src = await admission.ready(past_posts_engine, origin)
    worker, job, c = await leased(world_db, past_posts_engine, origin, src)
    try:
        before = await state(past_posts_engine, origin, src)
        with pytest.raises(RuntimeError, match="injected landing fault"):
            await m.run_sync(
                worker,
                job,
                ports=await ports(m, Adapter(page(obs.bundle())), c),
                fault=fault_at(stage),
            )
        assert await state(past_posts_engine, origin, src) == before
        result = await m.run_sync(
            worker, job, ports=await ports(m, Adapter(page(obs.bundle())), c)
        )
        assert result.committed_observations == 1
    finally:
        await worker.dispose()


def told(caplog, module):
    """What the sync logged about the posts a turn changed, in order."""
    return [r.getMessage() for r in caplog.records if r.name == module.__name__]


async def test_a_slide_the_landing_holds_out_is_told_once_its_turn_commits(
    world_db, past_posts_engine, origin, caplog
):
    """The slide line is written after the turn commits, so a turn rolled back
    at its last step tells nothing. The committed one names the post, the
    number of slides its read listed and what the landing did with the one
    the read lacks."""
    m = service()
    src = await admission.ready(past_posts_engine, origin)
    worker, job, c = await leased(world_db, past_posts_engine, origin, src)
    await obs.observe(
        past_posts_engine, origin, src, slides.carousel(ids=(100, 101, 102))
    )
    lacking = page(slides.carousel(ids=(100, 101)))
    try:
        with caplog.at_level(logging.WARNING, logger=m.__name__):
            with pytest.raises(RuntimeError, match="injected landing fault"):
                await m.run_sync(
                    worker,
                    job,
                    ports=await ports(m, Adapter(lacking), c),
                    fault=fault_at("before_commit"),
                )
            assert told(caplog, m) == []
            result = await m.run_sync(
                worker, job, ports=await ports(m, Adapter(lacking), c)
            )
        assert result.committed_observations == 1
        assert told(caplog, m) == [
            "instagram sync: post 900, a complete read of 2 slide(s): kept [] for"
            " the stories that hold them, held ['102'] out of the draw, retired []"
        ]
    finally:
        await worker.dispose()


@pytest.mark.parametrize("mutation", ["off", "off_on", "generation"])
async def test_held_page_never_lands_across_source_fence(
    world_db, past_posts_engine, origin, mutation
):
    m = service()
    src = await admission.ready(past_posts_engine, origin)
    worker, job, c = await leased(world_db, past_posts_engine, origin, src)
    try:
        before = await state(past_posts_engine, origin, src)

        async def change():
            if mutation in ("off", "off_on"):
                from src.services.target import source_state

                for ratio in (0, 1) if mutation == "off_on" else (0,):
                    async with obs.uow(past_posts_engine, origin) as conn:
                        await source_state.lock_all_workspace_sources(
                            conn, workspace_id=str(origin["ws"])
                        )
                        before_state = await source_state.past_posts_state(
                            conn,
                            workspace_id=str(origin["ws"]),
                            source_id=src["source_id"],
                        )
                        await conn.execute(
                            text(
                                "UPDATE category_post_case_mix SET ratio=:ratio WHERE source_id=:sid"
                            ),
                            dict(ratio=ratio, sid=src["source_id"]),
                        )
                        await source_state.apply_past_posts_transition(
                            conn,
                            workspace_id=str(origin["ws"]),
                            source_id=src["source_id"],
                            before=before_state,
                            reason="test",
                        )
            if mutation == "generation":
                await foundation.ensure(
                    past_posts_engine, origin, foundation.authorization()
                )

        adapter = Adapter(page(obs.bundle()), change)
        result = await m.run_sync(worker, job, ports=await ports(m, adapter, c))
        assert result.outcome == "dropped" and result.drop_reason in (
            "source_off",
            "source_epoch_changed",
            "source_generation_changed",
        )
        assert await state(past_posts_engine, origin, src) == before
        assert adapter.calls == 1
    finally:
        await worker.dispose()


async def test_missing_capture_refuses_before_provider(
    world_db, past_posts_engine, origin
):
    m = service()
    src = await admission.ready(past_posts_engine, origin)
    worker, job, c = await leased(world_db, past_posts_engine, origin, src)
    try:
        job = {
            **job,
            "payload": {
                k: v for k, v in job["payload"].items() if k != "source_read_capture"
            },
        }
        adapter = Adapter(page())
        result = await m.run_sync(worker, job, ports=await ports(m, adapter, c))
        assert result.drop_reason == "source_capture_missing" and adapter.calls == 0
    finally:
        await worker.dispose()


async def next_job(worker, origin, previous):
    async with obs.uow(worker, origin) as conn:
        await jobs.finalize_job(
            conn, previous["id"], previous["lease_token"], "succeeded"
        )
    async with worker.connect() as conn:
        return await jobs.claim_job(
            conn,
            lane="bulk",
            worker="past-posts-sync-test",
            lease_seconds=60,
            ws_lane_cap=4,
            capability=foundation.capability(),
        )


class EnrichingAdapter(Adapter):
    async def enrich_parents(self, parent_media_ids, *, source_id, workspace_id):
        self.calls += 1
        return MediaPage(
            [],
            {},
            observations=tuple(
                obs.bundle(parent, attribution="not_detected")
                for parent in parent_media_ids
            ),
            counters=MediaTurnCounters(
                graph_attempts=len(parent_media_ids),
                observed_parents=len(parent_media_ids),
            ),
            outcome="parent_enriched",
        )


async def test_supplementary_evidence_cannot_survive_reobservation(
    past_posts_engine, origin
):
    src = await admission.ready(past_posts_engine, origin)
    await obs.observe(past_posts_engine, origin, src, obs.bundle())
    async with obs.uow(past_posts_engine, origin) as conn:
        await conn.execute(
            text(
                "UPDATE instagram_media_assets SET validation_capture=jsonb_build_object('generation_id',data_generation_id,'observation_revision',observation_revision,'selected_asset_id',asset_media_id,'state','validation_pending') WHERE source_id=:sid"
            ),
            dict(sid=src["source_id"]),
        )
    await obs.observe(past_posts_engine, origin, src, obs.bundle())
    async with obs.uow(past_posts_engine, origin) as conn:
        assert (
            await conn.execute(
                text(
                    "SELECT validation_capture FROM instagram_media_assets WHERE source_id=:sid"
                ),
                dict(sid=src["source_id"]),
            )
        ).scalar_one() is None


async def test_lease_loss_during_provider_rolls_back_without_continuation(
    world_db, past_posts_engine, origin
):
    m = service()
    src = await admission.ready(past_posts_engine, origin)
    worker, job, c = await leased(world_db, past_posts_engine, origin, src)
    try:
        before = await state(past_posts_engine, origin, src)

        async def cancel():
            async with obs.uow(worker, origin) as conn:
                await jobs.finalize_job(conn, job["id"], job["lease_token"], "failed")

        with pytest.raises(jobs.JobFenced):
            await m.run_sync(
                worker,
                job,
                ports=await ports(m, Adapter(page(obs.bundle()), cancel), c),
            )
        assert await state(past_posts_engine, origin, src) == before
    finally:
        await worker.dispose()


async def test_late_persistent_error_is_source_fenced(
    world_db, past_posts_engine, origin
):
    from src.services.target.media_adapters import MediaCredentialDead

    m = service()
    src = await admission.ready(past_posts_engine, origin)
    worker, job, c = await leased(world_db, past_posts_engine, origin, src)
    try:
        before = await state(past_posts_engine, origin, src)

        async def late_error():
            async with obs.uow(past_posts_engine, origin) as conn:
                await conn.execute(
                    text(
                        "UPDATE category_post_case_mix SET ratio=0 WHERE source_id=:sid"
                    ),
                    dict(sid=src["source_id"]),
                )
            raise MediaCredentialDead("secret provider detail")

        result = await m.run_sync(
            worker, job, ports=await ports(m, Adapter(page(), late_error), c)
        )
        assert result.drop_reason == "source_off"
        assert await state(past_posts_engine, origin, src) == before
        async with obs.uow(past_posts_engine, origin) as conn:
            row = await obs.row(
                conn,
                "SELECT state,alerted_at FROM media_sources WHERE id=:sid",
                sid=src["source_id"],
            )
            assert row["state"] == "active" and row["alerted_at"] is None
    finally:
        await worker.dispose()


async def test_real_metadata_indexing_never_downloads_or_decodes(
    world_db, past_posts_engine, origin, monkeypatch
):
    from datetime import datetime, timedelta, timezone
    import httpx
    from src.services.target import (
        egress,
        instagram_past_posts_adapter as past_posts,
        media_validation,
    )
    from src.services.target.unit_of_work import in_transaction

    m = service()
    account = "17841400000000001"
    async with obs.uow(past_posts_engine, origin) as conn:
        await conn.execute(
            text("UPDATE ig_accounts SET provider_account_ref=:ref WHERE id=:id"),
            dict(ref=account, id=str(origin["iga"])),
        )
    src = await admission.ready(past_posts_engine, origin)
    worker, job, c = await leased(world_db, past_posts_engine, origin, src)
    now = datetime.now(timezone.utc)
    decoder_calls = []

    async def forbidden_decoder(*args, **kwargs):
        decoder_calls.append(True)
        pytest.fail("metadata indexing invoked a media validator/decoder")

    monkeypatch.setattr(media_validation, "validate_media", forbidden_decoder)
    monkeypatch.setattr(media_validation, "run_bounded", forbidden_decoder)
    rows = [
        dict(
            id="111",
            timestamp=(now - timedelta(days=20)).isoformat(),
            media_type="IMAGE",
            media_url="https://scontent-test.cdninstagram.com/111.jpg",
        ),
        dict(
            id="222",
            timestamp=(now - timedelta(days=120)).isoformat(),
            media_type="IMAGE",
            media_url="https://scontent-test.cdninstagram.com/222.jpg",
        ),
        dict(
            id="333",
            timestamp=(now - timedelta(days=120)).isoformat(),
            media_type="UNKNOWN",
        ),
    ]
    requests = []

    async def handler(request):
        assert not in_transaction(), "provider call held a tenant transaction"
        requests.append((request.url.host, request.url.path))
        assert request.url.host == "graph.instagram.com", (
            "metadata indexing downloaded media from CDN"
        )
        path = request.url.path.rsplit("/", 1)[-1]
        if path == "media":
            # The walk's gap probe asks for `until` alone: the newest post below it.
            since, until = (
                int(request.url.params.get("since", "0")),
                int(request.url.params["until"]),
            )
            return httpx.Response(
                200,
                json={
                    "data": [
                        r
                        for r in rows
                        if since
                        <= datetime.fromisoformat(r["timestamp"]).timestamp()
                        < until
                    ]
                },
            )
        selected = next(r for r in rows if r["id"] == path)
        return httpx.Response(
            200,
            json={
                **selected,
                "owner": {"id": account},
                "caption": "Original photograph",
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def prepare(capture, provider):
        assert capture == c

        async def access(source_id, *, workspace_id):
            return past_posts.PastPostsAccess(
                account, "fake-internal-test-token", captured_authorization=capture
            )

        return past_posts.InstagramPastPostsAdapter(
            access_provider=access,
            admission_provider=provider,
            client=client,
            graph_version="v26.0",
            page_size=100,
            metadata_only=True,
            policy=egress.EgressPolicy().without(enforce_private_address_block=False),
        )

    injected = m.PastPostsSyncPorts(
        capability=foundation.capability(),
        tombstone_keys=obs.keys(),
        prepare_adapter=prepare,
    )
    try:
        results = []
        for turn in range(5):
            results.append(await m.run_sync(worker, job, ports=injected))
            cp, retained, queued = await state(past_posts_engine, origin, src)
            if turn < 4:
                job = await next_job(worker, origin, job)
        # The fifth turn found its window empty, and one probe found nothing older.
        assert cp["history"]["complete"] and not cp["history"]["checkpoint"]["more"]
        assert cp["summary"]["observed"] == 3 and cp["summary"]["represented"] == 2
        assert cp["summary"]["eligible"] == 0, (
            "metadata indexing does not byte-verify readiness"
        )
        assert cp["summary"]["exclusions"]["unsupported"] == 1
        assert "pending_validation" not in cp and "validation" not in cp["progress"]
        assert not decoder_calls
        assert all(
            r.lane in ("recent", "history", "helper") and r.cdn_attempts == 0
            for r in results
        )
        assert all(r.graph_attempts <= 20 for r in results)
        async with obs.uow(worker, origin) as conn:
            old = (
                (
                    await conn.execute(
                        text(
                            "SELECT i.original_at,i.attribution,a.validation_state,m.media_kind,m.provider_file_ref FROM instagram_media_index i JOIN instagram_media_assets a ON a.workspace_id=i.workspace_id AND a.source_id=i.source_id AND a.parent_media_id=i.parent_media_id JOIN media_items m ON m.id=a.media_item_id AND m.workspace_id=a.workspace_id WHERE i.source_id=:sid AND i.parent_media_id='222'"
                        ),
                        dict(sid=src["source_id"]),
                    )
                )
                .mappings()
                .one()
            )
            assert old["original_at"] < now - timedelta(days=90)
            assert (
                old["attribution"] == "not_detected"
                and old["media_kind"] == "image"
                and old["provider_file_ref"] == "222"
            )
            assert old["validation_state"] == "pending"
        assert sum(r.graph_attempts for r in results) == len(requests)
        assert all(r.outcome == "committed" for r in results)
        actual_graph = sum(host == "graph.instagram.com" for host, _ in requests)
        actual_cdn = sum(
            host == "scontent-test.cdninstagram.com" for host, _ in requests
        )
        assert sum(r.cdn_attempts for r in results) == actual_cdn
        print(
            "PAST_POSTS_SYNC_SAMPLE="
            + json.dumps(
                {
                    "provider": "injected_fake_http",
                    "decoding_calls": len(decoder_calls),
                    "indexing": "metadata_only",
                    "turns": len(results),
                    "lanes": [r.lane for r in results],
                    "observed": cp["summary"]["observed"],
                    "represented": cp["summary"]["represented"],
                    "eligible": cp["summary"]["eligible"],
                    "history_complete": cp["history"]["complete"],
                    "actual_graph_attempts": actual_graph,
                    "reported_graph_attempts": sum(r.graph_attempts for r in results),
                    "actual_cdn_attempts": actual_cdn,
                    "reported_cdn_attempts": sum(r.cdn_attempts for r in results),
                },
                sort_keys=True,
            )
        )
    finally:
        await client.aclose()
        await worker.dispose()


@pytest.mark.parametrize("bad", ["missing", "null", "generation", "asset"])
async def test_validation_capture_cannot_name_another_or_missing_identity(
    past_posts_engine, origin, bad
):
    from sqlalchemy.exc import DBAPIError

    src = await admission.ready(past_posts_engine, origin)
    await obs.observe(past_posts_engine, origin, src, obs.bundle())
    capture = dict(
        generation_id=src["generation_id"],
        observation_revision=1,
        selected_asset_id="123",
        state="validation_pending",
    )
    if bad == "missing":
        del capture["selected_asset_id"]
    if bad == "null":
        capture["generation_id"] = None
    if bad == "generation":
        capture["generation_id"] = "00000000-0000-0000-0000-000000000000"
    if bad == "asset":
        capture["selected_asset_id"] = "999"
    import json

    with pytest.raises(DBAPIError):
        async with obs.uow(past_posts_engine, origin) as conn:
            await conn.execute(
                text(
                    "UPDATE instagram_media_assets SET validation_capture=CAST(:capture AS jsonb) WHERE source_id=:sid"
                ),
                dict(sid=src["source_id"], capture=json.dumps(capture)),
            )


async def test_media_executor_uses_injected_past_posts_turn(
    world_db, past_posts_engine, origin
):
    from src.services.target import media_sync, work_loop

    m = service()
    src = await admission.ready(past_posts_engine, origin)
    worker, job, c = await leased(world_db, past_posts_engine, origin, src)
    try:
        adapter = Adapter(page(obs.bundle()))
        deps = work_loop.WorkerDeps(
            engine=worker, past_posts_sync=await ports(m, adapter, c)
        )
        assert await media_sync.sync_media_source(deps, None, job) == "committed"
        cp, rows, queued = await state(past_posts_engine, origin, src)
        assert len(rows) == 1 and cp["revision"] == 1 and queued == 1
    finally:
        await worker.dispose()


async def test_same_checkpoint_race_commits_once(world_db, past_posts_engine, origin):
    import asyncio

    m = service()
    src = await admission.ready(past_posts_engine, origin)
    worker, job, c = await leased(world_db, past_posts_engine, origin, src)
    try:
        arrivals = 0
        both = asyncio.Event()

        async def held():
            nonlocal arrivals
            arrivals += 1
            if arrivals == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 3)

        injected = await ports(m, Adapter(page(obs.bundle()), held), c)
        results = await asyncio.gather(
            m.run_sync(worker, job, ports=injected),
            m.run_sync(worker, job, ports=injected),
        )
        assert sorted(r.outcome for r in results) == ["committed", "dropped"]
        assert (
            next(r for r in results if r.outcome == "dropped").drop_reason
            == "checkpoint_changed"
        )
        cp, rows, queued = await state(past_posts_engine, origin, src)
        assert cp["revision"] == 1 and len(rows) == 1 and queued == 1
    finally:
        await worker.dispose()


async def test_completed_lanes_rearm_baseline_and_new_sync_refreshes(
    world_db, past_posts_engine, origin
):
    m = service()
    src = await admission.ready(past_posts_engine, origin)
    worker, job, c = await leased(world_db, past_posts_engine, origin, src)
    try:
        empty = MediaPage([], {"v": 2, "more": False}, outcome="metadata_page")
        adapter = Adapter(empty)
        injected = await ports(m, adapter, c)
        await m.run_sync(worker, job, ports=injected)
        job = await next_job(worker, origin, job)
        await m.run_sync(worker, job, ports=injected)
        cp, rows, queued = await state(past_posts_engine, origin, src)
        assert cp["recent"]["complete"] and cp["history"]["complete"] and queued == 0
        assert not cp["summary"]["indexing"] and not cp["summary"]["partial"]
        assert not cp["summary"]["stalled"]
        async with obs.uow(worker, origin) as conn:
            assert (
                await conn.execute(
                    text(
                        "SELECT next_sync_at>clock_timestamp() FROM media_sources WHERE id=:sid"
                    ),
                    dict(sid=src["source_id"]),
                )
            ).scalar_one()
            await jobs.finalize_job(conn, job["id"], job["lease_token"], "succeeded")
            await jobs.enqueue(
                conn,
                kind="sync_media_source",
                workspace_id=str(origin["ws"]),
                serialization_key="src:" + src["source_id"],
                payload={
                    "v": 1,
                    "source_id": src["source_id"],
                    "source_read_capture": c.payload(),
                },
            )
        async with worker.connect() as conn:
            fresh = await jobs.claim_job(
                conn,
                lane="bulk",
                worker="fresh",
                lease_seconds=60,
                ws_lane_cap=4,
                capability=foundation.capability(),
            )

        async with obs.uow(worker, origin) as conn:
            await conn.execute(
                text(
                    "INSERT INTO instagram_media_index(workspace_id,source_id,parent_media_id,data_generation_id,original_at,availability,exclusion_reason) SELECT :ws,:sid,(500000+n)::text,CAST(:gid AS uuid),'2020-01-01'::timestamptz,'excluded','unsupported_media' FROM generate_series(1,10000) n"
                ),
                dict(ws=str(origin["ws"]), sid=src["source_id"], gid=c.generation_id),
            )

        class Fresh(Adapter):
            async def list_changes(
                self, config, checkpoint, *, source_id, workspace_id
            ):
                assert checkpoint.get("more") is True and checkpoint.get("seen") == 0
                return page(obs.bundle("999999"))

        await m.run_sync(worker, fresh, ports=await ports(m, Fresh(empty), c))
        cp, rows, queued = await state(past_posts_engine, origin, src)
        assert len(rows) == 10001 and cp["summary"]["observed"] == 10001
    finally:
        await worker.dispose()


async def test_missing_parent_enrichment_yields_to_baseline_after_finite_work(
    world_db, past_posts_engine, origin
):
    m = service()
    src = await admission.ready(past_posts_engine, origin)
    worker, job, c = await leased(world_db, past_posts_engine, origin, src)

    class Missing(Adapter):
        async def enrich_parents(self, parent_media_ids, **kwargs):
            return MediaPage(
                [],
                {},
                outcome="parent_enrichment_pending",
                deferred=tuple(parent_media_ids),
            )

    try:
        adapter = Missing(page(obs.bundle()))
        injected = await ports(m, adapter, c)
        await m.run_sync(worker, job, ports=injected)
        job = await next_job(worker, origin, job)
        await m.run_sync(worker, job, ports=injected)
        job = await next_job(worker, origin, job)
        adapter.page = MediaPage([], {"v": 2, "more": False})
        await m.run_sync(worker, job, ports=injected)
        cp, rows, queued = await state(past_posts_engine, origin, src)
        assert queued == 0 and cp["deferred_helpers"] == ["123"]
        async with obs.uow(worker, origin) as conn:
            assert (
                await conn.execute(
                    text(
                        "SELECT next_sync_at>clock_timestamp() FROM media_sources WHERE id=:sid"
                    ),
                    dict(sid=src["source_id"]),
                )
            ).scalar_one()
    finally:
        await worker.dispose()


async def test_selected_child_change_clears_all_inherited_format_evidence(
    world_db, past_posts_engine, origin
):
    src = await admission.ready(past_posts_engine, origin)
    worker, job, c = await leased(world_db, past_posts_engine, origin, src)
    try:
        await obs.observe(past_posts_engine, origin, src, obs.bundle())
        async with obs.uow(worker, origin) as conn:
            await conn.execute(
                text(
                    "UPDATE instagram_media_assets SET validation_state='supported',validation_generation_id=data_generation_id,validation_observation_revision=observation_revision,validation_fingerprint='prior-format-fixture',validated_at=clock_timestamp(),validation_capture=jsonb_build_object('generation_id',data_generation_id,'observation_revision',observation_revision,'selected_asset_id',asset_media_id,'state','supported') WHERE source_id=:sid"
                ),
                dict(sid=src["source_id"]),
            )
            await conn.execute(
                text(
                    "UPDATE instagram_media_assets SET asset_media_id='456' WHERE source_id=:sid"
                ),
                dict(sid=src["source_id"]),
            )
            row = await obs.row(
                conn,
                "SELECT validation_state,validation_fingerprint,validation_capture FROM instagram_media_assets WHERE source_id=:sid",
                sid=src["source_id"],
            )
            assert (
                row["validation_state"] == "pending"
                and row["validation_fingerprint"] is None
                and row["validation_capture"] is None
            )
    finally:
        await worker.dispose()


async def test_index_only_conflict_helper_finishes_metadata(
    world_db, past_posts_engine, origin
):
    m = service()
    src = await admission.ready(past_posts_engine, origin)
    async with obs.uow(past_posts_engine, origin) as conn:
        await conn.execute(
            text(
                "INSERT INTO media_items (workspace_id, source_id, content_hash, file_name, media_kind, provider_file_ref) VALUES (:ws,:sid,'instagram_past_posts:media:123','retained.jpg','image','123')"
            ),
            dict(ws=str(origin["ws"]), sid=str(origin["src"])),
        )
    worker, job, c = await leased(world_db, past_posts_engine, origin, src)
    try:
        adapter = EnrichingAdapter(page(obs.bundle()))
        injected = await ports(m, adapter, c)
        await m.run_sync(worker, job, ports=injected)
        job = await next_job(worker, origin, job)
        await m.run_sync(worker, job, ports=injected)
        cp, rows, queued = await state(past_posts_engine, origin, src)
        assert rows[0].media_item_id is None
        assert "pending_validation" not in cp
    finally:
        await worker.dispose()


@pytest.mark.parametrize(
    "history_seen", [0, 1, "counter_behind_lane", "raw_v1", "raw_v2"]
)
async def test_recent_history_share_one_adapter_listing_ceiling(
    world_db, past_posts_engine, origin, history_seen
):
    from datetime import datetime, timezone
    import httpx
    from src.services.target import egress, instagram_past_posts_adapter as past_posts

    m = service()
    async with obs.uow(past_posts_engine, origin) as conn:
        await conn.execute(
            text(
                "UPDATE ig_accounts SET provider_account_ref='17841400000000001' WHERE id=:id"
            ),
            dict(id=str(origin["iga"])),
        )
    src = await admission.ready(past_posts_engine, origin)
    worker, job, c = await leased(world_db, past_posts_engine, origin, src)
    upper = int(datetime.now(timezone.utc).timestamp()) + 1
    cp = m._envelope(None, c)
    cp.pop("scan_seen", None)  # Existing envelopes have separate lane counters.
    watermark = dict(
        v=2,
        upper=upper,
        until=upper,
        window_start=upper - past_posts.WINDOW_SECONDS,
        boundary=[],
        seen=9999,
        more=True,
        ceiling_reached=False,
    )
    cp["recent"] = dict(checkpoint=watermark, complete=True)
    cp["history"] = dict(
        checkpoint={
            **watermark,
            "seen": history_seen if isinstance(history_seen, int) else 1,
        },
        complete=False,
    )
    if history_seen == "counter_behind_lane":
        cp["scan_seen"] = 9998
    cp["next_lane"] = cp["next_metadata"] = "history"
    raw = None
    if history_seen in ("raw_v1", "raw_v2"):
        raw = {
            **watermark,
            "v": int(history_seen[-1]),
            "upper": upper - past_posts.WINDOW_SECONDS,
            "until": upper - past_posts.WINDOW_SECONDS - 10,
            "window_start": upper - 2 * past_posts.WINDOW_SECONDS - 10,
            "boundary": ["777"],
        }
        cp = raw
    async with obs.uow(worker, origin) as conn:
        await conn.execute(
            text(
                "UPDATE media_sources SET sync_checkpoint=CAST(:cp AS jsonb) WHERE id=:sid"
            ),
            dict(cp=json.dumps(cp), sid=src["source_id"]),
        )
    requests = []

    async def handler(request):
        requests.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "data": [
                    dict(
                        id="444",
                        timestamp=datetime.fromtimestamp(
                            upper - 1, timezone.utc
                        ).isoformat(),
                        media_type="UNKNOWN",
                    ),
                    dict(
                        id="445",
                        timestamp=datetime.fromtimestamp(
                            upper - 1, timezone.utc
                        ).isoformat(),
                        media_type="UNKNOWN",
                    ),
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def prepare(capture, provider):
        async def access(source_id, *, workspace_id):
            return past_posts.PastPostsAccess(
                capture.account_ref,
                "fake-internal-test-token",
                captured_authorization=capture,
            )

        return past_posts.InstagramPastPostsAdapter(
            access_provider=access,
            admission_provider=provider,
            client=client,
            graph_version="v26.0",
            metadata_only=True,
            policy=egress.EgressPolicy().without(enforce_private_address_block=False),
        )

    try:
        if history_seen == "counter_behind_lane":
            with pytest.raises(m.PastPostsCheckpointRefused):
                await m.run_sync(
                    worker,
                    job,
                    ports=m.PastPostsSyncPorts(
                        foundation.capability(), obs.keys(), prepare
                    ),
                )
            assert requests == []
            return
        result = await m.run_sync(
            worker,
            job,
            ports=m.PastPostsSyncPorts(foundation.capability(), obs.keys(), prepare),
        )
        cp, rows, queued = await state(past_posts_engine, origin, src)
        if raw is not None:
            assert result.committed_observations == 1 and len(requests) == 1
            assert cp["scan_seen"] == 10000
            assert {
                key: cp["history"]["checkpoint"][key]
                for key in ("upper", "until", "window_start", "boundary")
            } == {
                key: raw[key] for key in ("upper", "until", "window_start", "boundary")
            }
        else:
            assert result.committed_observations == 1 - history_seen
            assert len(requests) == 1 - history_seen
        assert cp["recent"]["complete"] and cp["history"]["complete"]
        assert cp["summary"]["partial"]
        assert cp["history"]["checkpoint"]["ceiling_reached"]
        if history_seen == 1:
            assert queued == 0 and not cp["summary"]["indexing"]
    finally:
        await client.aclose()
        await worker.dispose()


async def quality_ports(module, capture, response):
    import httpx
    from src.services.target import egress, instagram_past_posts_adapter as past_posts
    from src.services.target.unit_of_work import in_transaction

    requests = []

    async def handler(request):
        assert not in_transaction()
        requests.append((request.url.host, request.url.path))
        return await response(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def prepare(captured, provider):
        assert captured == capture

        async def access(source_id, *, workspace_id):
            from src.services.target.instagram_past_posts_adapter import PastPostsAccess

            return PastPostsAccess(
                capture.account_ref, "fake-test-token", captured_authorization=capture
            )

        return past_posts.InstagramPastPostsAdapter(
            access_provider=access,
            admission_provider=provider,
            client=client,
            graph_version="v26.0",
            metadata_only=True,
            policy=egress.EgressPolicy().without(enforce_private_address_block=False),
        )

    return (
        module.PastPostsSyncPorts(foundation.capability(), obs.keys(), prepare),
        client,
        requests,
    )


async def quality_source(world_db, engine, origin):
    async with obs.uow(engine, origin) as conn:
        await conn.execute(
            text(
                "UPDATE ig_accounts SET provider_account_ref='17841400000000001' WHERE id=:id"
            ),
            dict(id=str(origin["iga"])),
        )
    src = await admission.ready(engine, origin)
    worker, job, capture = await leased(world_db, engine, origin, src)
    return src, worker, job, capture


async def quality_checkpoint(
    module,
    worker,
    origin,
    src,
    capture,
    *,
    lane,
    helpers=(),
    validation=(),
    candidates=None,
    history_done=False,
):
    from datetime import datetime, timezone
    from src.services.target.instagram_past_posts_adapter import WINDOW_SECONDS

    upper = int(datetime.now(timezone.utc).timestamp()) + 1
    cp = module._envelope(None, capture)
    watermark = dict(
        v=2,
        upper=upper,
        until=upper,
        window_start=upper - WINDOW_SECONDS,
        boundary=[],
        seen=0,
        more=True,
        ceiling_reached=False,
    )
    cp["recent"] = dict(checkpoint={**watermark, "more": False}, complete=True)
    cp["history"] = dict(
        checkpoint={
            **watermark,
            "until": upper - WINDOW_SECONDS,
            "window_start": upper - 2 * WINDOW_SECONDS,
        },
        complete=False,
    )
    if history_done:
        # Both lanes done, so only helper turns follow, one after another.
        cp["history"] = dict(
            checkpoint={**cp["history"]["checkpoint"], "more": False}, complete=True
        )
    if candidates is not None:
        # What the last committed turn stored; a helper turn is sized by it.
        cp["summary"] = {"candidates": candidates}
    cp.update(
        next_lane=lane,
        next_metadata="history",
        pending_helpers=list(helpers),
    )
    async with obs.uow(worker, origin) as conn:
        await conn.execute(
            text(
                "UPDATE media_sources SET sync_checkpoint=CAST(:cp AS jsonb) WHERE id=:sid"
            ),
            dict(cp=json.dumps(cp), sid=src["source_id"]),
        )
    return cp


async def refused_parent(world_db, engine, origin):
    """A helper turn queued for post 111, whose read Instagram refuses for good."""
    import httpx

    m = service()
    src, worker, job, c = await quality_source(world_db, engine, origin)
    await obs.observe(
        engine, origin, src, obs.bundle("111", attribution="not_detected")
    )
    await quality_checkpoint(m, worker, origin, src, c, lane="helper", helpers=["111"])

    async def response(request):
        if request.url.path.endswith("/media"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(
            400, json={"error": {"code": 100, "message": "private parent failure"}}
        )

    injected, client, requests = await quality_ports(m, c, response)
    return m, src, worker, job, c, injected, client, requests


async def test_real_permanent_helper_failure_retires_without_partial_observation(
    world_db, past_posts_engine, origin
):
    m, src, worker, job, c, injected, client, requests = await refused_parent(
        world_db, past_posts_engine, origin
    )
    try:
        first = await m.run_sync(worker, job, ports=injected)
        cp, rows, queued = await state(past_posts_engine, origin, src)
        assert first.outcome == "committed" and first.graph_attempts == 1
        assert (
            not cp["pending_helpers"]
            and cp["deferred_helpers"] == []
            and cp["next_lane"] == "history"
        )
        async with obs.uow(worker, origin) as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT i.availability,i.observation_revision,a.availability AS asset_availability,a.exclusion_reason,a.validation_state,a.validation_capture FROM instagram_media_index i JOIN instagram_media_assets a ON a.workspace_id=i.workspace_id AND a.source_id=i.source_id AND a.parent_media_id=i.parent_media_id WHERE i.source_id=:sid"
                        ),
                        dict(sid=src["source_id"]),
                    )
                )
                .mappings()
                .one()
            )
            assert (
                row["availability"] == "unavailable"
                and row["observation_revision"] == 1
                and row["validation_state"] == "pending"
            )
            assert row["asset_availability"] == "unavailable"
            assert row["exclusion_reason"] == "asset_unavailable"
            assert row["validation_capture"] is None
        job = await next_job(worker, origin, job)
        assert (await m.run_sync(worker, job, ports=injected)).lane == "history"
        # The empty window and its one probe (nothing older) end the history lane,
        # and with it the scan: no job follows until a baseline sync re-arms it.
        assert [path.rsplit("/", 1)[1] for _, path in requests[1:]] == ["media"] * 2
        cp, _, queued = await state(past_posts_engine, origin, src)
        assert cp["recent"]["complete"] and cp["history"]["complete"] and queued == 0
        async with obs.uow(worker, origin) as conn:
            await jobs.finalize_job(conn, job["id"], job["lease_token"], "succeeded")
            await jobs.enqueue(
                conn,
                kind="sync_media_source",
                workspace_id=str(origin["ws"]),
                serialization_key="src:" + src["source_id"],
                payload={
                    "v": 1,
                    "source_id": src["source_id"],
                    "source_read_capture": c.payload(),
                },
            )
        async with worker.connect() as conn:
            job = await jobs.claim_job(
                conn,
                lane="bulk",
                worker="past-posts-sync-test",
                lease_seconds=60,
                ws_lane_cap=4,
                capability=foundation.capability(),
            )
        assert (await m.run_sync(worker, job, ports=injected)).lane == "recent"
        # A baseline rewalk with no parent to enrich must not resurrect the
        # terminal helper or queue a second read of its failed parent.
        assert await next_job(worker, origin, job) is None
        cp, _, queued = await state(past_posts_engine, origin, src)
        assert cp["pending_helpers"] == cp["deferred_helpers"] == []
        assert queued == 0
        assert [path.rsplit("/", 1)[1] for _, path in requests].count("111") == 1
        async with obs.uow(worker, origin) as conn:
            assert (
                await conn.execute(
                    text(
                        "SELECT next_sync_at>clock_timestamp() FROM media_sources WHERE id=:sid"
                    ),
                    dict(sid=src["source_id"]),
                )
            ).scalar_one()
    finally:
        await client.aclose()
        await worker.dispose()


async def test_a_post_a_refused_read_retires_is_told_once_its_turn_commits(
    world_db, past_posts_engine, origin, caplog
):
    """The post line is written after the turn commits, so a turn rolled back
    at its last step tells nothing. The committed one says what retired the
    post, a read of it that Instagram refused for good, and how many slide
    items that removed."""
    m, _src, worker, job, _c, injected, client, _requests = await refused_parent(
        world_db, past_posts_engine, origin
    )
    try:
        with caplog.at_level(logging.WARNING, logger=m.__name__):
            with pytest.raises(RuntimeError, match="injected landing fault"):
                await m.run_sync(
                    worker, job, ports=injected, fault=fault_at("before_commit")
                )
            assert told(caplog, m) == []
            result = await m.run_sync(worker, job, ports=injected)
        assert result.outcome == "committed"
        assert told(caplog, m) == [
            "instagram sync: post 111 retired: Instagram refused a read of it for"
            " good; 1 slide item(s) removed"
        ]
    finally:
        await client.aclose()
        await worker.dispose()


def parent_json(request, **extra):
    """The Graph's answer for one queued parent: an old image, owned and captioned."""
    return {
        "id": request.url.path.rsplit("/", 1)[1],
        "timestamp": "2020-01-01T00:00:00+0000",
        "media_type": "IMAGE",
        "media_url": "https://scontent-test.cdninstagram.com/parent.jpg",
        "owner": {"id": "17841400000000001"},
        "caption": "Original",
        **extra,
    }


async def batch_state(engine, origin, src):
    async with obs.uow(engine, origin) as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT i.parent_media_id,i.attribution,a.availability,a.exclusion_reason,a.validation_capture FROM instagram_media_index i JOIN instagram_media_assets a ON a.workspace_id=i.workspace_id AND a.source_id=i.source_id AND a.parent_media_id=i.parent_media_id WHERE i.source_id=:sid ORDER BY i.parent_media_id"
                    ),
                    dict(sid=src["source_id"]),
                )
            )
            .mappings()
            .all()
        )
    return {r["parent_media_id"]: r for r in rows}


async def test_a_helper_turn_enriches_every_parent_its_request_budget_affords(
    world_db, past_posts_engine, origin
):
    import httpx

    m = service()
    src, worker, job, c = await quality_source(world_db, past_posts_engine, origin)
    queued = [str(1001 + i) for i in range(25)]
    for parent in queued:
        await obs.observe(past_posts_engine, origin, src, obs.bundle(parent))
    await quality_checkpoint(m, worker, origin, src, c, lane="helper", helpers=queued)

    async def response(request):
        return httpx.Response(200, json=parent_json(request))

    injected, client, requests = await quality_ports(m, c, response)
    try:
        first = await m.run_sync(worker, job, ports=injected)
        cp, _, _ = await state(past_posts_engine, origin, src)
        assert first.outcome == "committed" and first.lane == "helper"
        # One request each: the 20-request turn reads twenty parents, in order,
        # and yields before a twenty-first request it cannot afford.
        assert first.graph_attempts == first.committed_observations == 20
        assert [path for _, path in requests] == [f"/v26.0/{p}" for p in queued[:20]]
        assert cp["pending_helpers"] == queued[20:] and cp["deferred_helpers"] == []
        rows = await batch_state(past_posts_engine, origin, src)
        assert {
            p for p, r in rows.items() if r["attribution"] == "not_detected"
        } == set(queued[:20])
        assert all(rows[p]["attribution"] == "unknown" for p in queued[20:])
    finally:
        await client.aclose()
        await worker.dispose()


async def test_a_helper_turn_reads_one_parent_until_the_source_has_a_candidate(
    world_db, past_posts_engine, origin
):
    """A turn commits only when it ends, and the first card waits for the first
    candidate. So while the stored summary reports none the turn reads a single
    parent, and the turn after it, which finds a candidate, reads a whole budget."""
    import httpx

    m = service()
    src, worker, job, c = await quality_source(world_db, past_posts_engine, origin)
    queued = [str(1001 + i) for i in range(25)]
    for parent in queued:
        await obs.observe(past_posts_engine, origin, src, obs.bundle(parent))
    await quality_checkpoint(
        m,
        worker,
        origin,
        src,
        c,
        lane="helper",
        helpers=queued,
        candidates=0,
        history_done=True,
    )

    async def response(request):
        return httpx.Response(200, json=parent_json(request))

    injected, client, requests = await quality_ports(m, c, response)
    try:
        # A continuation job: the finished lanes of a baseline job would restart.
        first = await m.run_sync(
            worker, {**job, "kind": "first_ingest_chunk"}, ports=injected
        )
        cp, _, _ = await state(past_posts_engine, origin, src)
        assert first.outcome == "committed" and first.lane == "helper"
        assert first.graph_attempts == first.committed_observations == 1
        assert [path for _, path in requests] == [f"/v26.0/{queued[0]}"]
        assert cp["summary"]["candidates"] == 1
        assert cp["pending_helpers"] == queued[1:]
        job = await next_job(worker, origin, job)
        requests.clear()
        second = await m.run_sync(worker, job, ports=injected)
        cp, _, _ = await state(past_posts_engine, origin, src)
        assert second.outcome == "committed" and second.lane == "helper"
        assert second.graph_attempts == second.committed_observations == 20
        assert [path for _, path in requests] == [f"/v26.0/{p}" for p in queued[1:21]]
        assert cp["pending_helpers"] == queued[21:]
    finally:
        await client.aclose()
        await worker.dispose()


async def test_a_source_with_no_candidate_reads_its_parents_in_doubling_turns(
    world_db, past_posts_engine, origin
):
    """Reading small must not cost a source that never has a candidate a turn per
    parent: the batch follows the requests the helper lane has made (1, 1, 2, 4,
    8) until it reaches what is left or a whole budget, and every parent is read."""
    import httpx

    m = service()
    src, worker, job, c = await quality_source(world_db, past_posts_engine, origin)
    queued = [str(1001 + i) for i in range(25)]
    for parent in queued:
        await obs.observe(past_posts_engine, origin, src, obs.bundle(parent))
    await quality_checkpoint(
        m,
        worker,
        origin,
        src,
        c,
        lane="helper",
        helpers=queued,
        candidates=0,
        history_done=True,
    )

    async def response(request):
        # Every parent credits another account, which the default policy excludes.
        return httpx.Response(
            200, json=parent_json(request, caption="Photo via @example_source")
        )

    injected, client, requests = await quality_ports(m, c, response)
    try:
        turns, claimed = [], job
        while claimed is not None:
            turns.append(
                await m.run_sync(
                    worker, {**claimed, "kind": "first_ingest_chunk"}, ports=injected
                )
            )
            cp, _, _ = await state(past_posts_engine, origin, src)
            assert cp["summary"]["candidates"] == 0
            claimed = await next_job(worker, origin, claimed)
        assert [t.graph_attempts for t in turns] == [1, 1, 2, 4, 8, 9]
        assert [t.committed_observations for t in turns] == [1, 1, 2, 4, 8, 9]
        assert [path for _, path in requests] == [f"/v26.0/{p}" for p in queued]
        assert cp["pending_helpers"] == [] and cp["deferred_helpers"] == []
        rows = await batch_state(past_posts_engine, origin, src)
        assert {r["attribution"] for r in rows.values()} == {"credited"}
    finally:
        await client.aclose()
        await worker.dispose()


async def test_one_bad_parent_does_not_starve_the_rest_of_a_batch(
    world_db, past_posts_engine, origin
):
    import httpx

    m = service()
    src, worker, job, c = await quality_source(world_db, past_posts_engine, origin)
    queued = ["111", "222", "333"]
    for parent in queued:
        await obs.observe(past_posts_engine, origin, src, obs.bundle(parent))
    await quality_checkpoint(m, worker, origin, src, c, lane="helper", helpers=queued)

    async def response(request):
        if request.url.path.endswith("/222"):
            return httpx.Response(
                400, json={"error": {"code": 100, "message": "private parent failure"}}
            )
        return httpx.Response(200, json=parent_json(request))

    injected, client, requests = await quality_ports(m, c, response)
    try:
        first = await m.run_sync(worker, job, ports=injected)
        cp, _, _ = await state(past_posts_engine, origin, src)
        assert first.graph_attempts == len(requests) == 3
        assert first.committed_observations == 2
        assert cp["pending_helpers"] == [] and cp["deferred_helpers"] == []
        rows = await batch_state(past_posts_engine, origin, src)
        assert (
            rows["111"]["attribution"] == rows["333"]["attribution"] == "not_detected"
        )
        assert rows["222"]["attribution"] == "unknown"
        assert rows["222"]["availability"] == "unavailable"
        assert rows["222"]["exclusion_reason"] == "asset_unavailable"
        assert rows["222"]["validation_capture"] is None
        assert rows["111"]["validation_capture"] is None
    finally:
        await client.aclose()
        await worker.dispose()


async def test_a_retryable_read_defers_its_parent_and_ends_the_batch(
    world_db, past_posts_engine, origin
):
    import httpx

    m = service()
    src, worker, job, c = await quality_source(world_db, past_posts_engine, origin)
    queued = ["111", "222", "333"]
    for parent in queued:
        await obs.observe(past_posts_engine, origin, src, obs.bundle(parent))
    await quality_checkpoint(m, worker, origin, src, c, lane="helper", helpers=queued)

    async def response(request):
        if request.url.path.endswith("/222"):
            return httpx.Response(
                429, json={"error": {"code": 4, "message": "private throttle"}}
            )
        return httpx.Response(200, json=parent_json(request))

    injected, client, requests = await quality_ports(m, c, response)
    try:
        first = await m.run_sync(worker, job, ports=injected)
        cp, _, _ = await state(past_posts_engine, origin, src)
        # A throttled provider is not asked for the third parent this turn.
        assert [path for _, path in requests] == ["/v26.0/111", "/v26.0/222"]
        assert first.graph_attempts == 2 and first.committed_observations == 1
        assert cp["pending_helpers"] == ["333"] and cp["deferred_helpers"] == ["222"]
        rows = await batch_state(past_posts_engine, origin, src)
        assert rows["222"]["validation_capture"] is None, "a retry is not a failure"
    finally:
        await client.aclose()
        await worker.dispose()


async def test_malformed_child_identity_defers_proven_carousel_without_retirement(
    world_db, past_posts_engine, origin
):
    import httpx
    from dataclasses import replace
    from src.services.target.instagram_catalog import PastPostsAsset

    m = service()
    src, worker, job, c = await quality_source(world_db, past_posts_engine, origin)
    known = replace(
        obs.bundle("111"),
        assets=(
            PastPostsAsset("101", "image", position=1),
            PastPostsAsset("102", "image", position=2),
            PastPostsAsset("103", "image", position=3),
        ),
        slide_count=3,
    )
    await obs.observe(past_posts_engine, origin, src, known)
    await quality_checkpoint(m, worker, origin, src, c, lane="helper", helpers=["111"])

    async def response(request):
        if request.url.path.endswith("/children"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "101",
                            "media_type": "IMAGE",
                            "media_url": "https://scontent-test.cdninstagram.com/one.jpg",
                        },
                        {
                            "media_type": "IMAGE",
                            "media_url": "https://scontent-test.cdninstagram.com/two.jpg",
                        },
                    ]
                },
            )
        return httpx.Response(
            200, json=parent_json(request, media_type="CAROUSEL_ALBUM")
        )

    injected, client, requests = await quality_ports(m, c, response)
    try:
        result = await m.run_sync(worker, job, ports=injected)
        cp, _, _ = await state(past_posts_engine, origin, src)
        assert result.committed_observations == 0
        assert cp["deferred_helpers"] == ["111"]
        assert cp["pending_helpers"] == []
        assert [path for _, path in requests] == ["/v26.0/111", "/v26.0/111/children"]
        async with obs.uow(past_posts_engine, origin) as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT a.asset_media_id,a.availability,m.state,i.availability AS post_state "
                            "FROM instagram_media_assets a JOIN media_items m "
                            "ON m.workspace_id=a.workspace_id AND m.id=a.media_item_id "
                            "JOIN instagram_media_index i ON i.workspace_id=a.workspace_id "
                            "AND i.source_id=a.source_id AND i.parent_media_id=a.parent_media_id "
                            "WHERE a.source_id=:sid ORDER BY a.position"
                        ),
                        {"sid": src["source_id"]},
                    )
                )
                .mappings()
                .all()
            )
        assert [r["asset_media_id"] for r in rows] == ["101", "102", "103"]
        assert all(
            r["availability"] == r["state"] == r["post_state"] == "available"
            for r in rows
        )
    finally:
        await client.aclose()
        await worker.dispose()


@pytest.mark.parametrize("kind", ["listing", "helper_fallback"])
@pytest.mark.parametrize("landing", ["error", "source", "checkpoint", "lease"])
async def test_real_grant_death_reports_actual_attempts(
    world_db, past_posts_engine, origin, kind, landing, monkeypatch
):
    import httpx

    m = service()
    src, worker, job, c = await quality_source(world_db, past_posts_engine, origin)
    if kind == "helper_fallback":
        await obs.observe(
            past_posts_engine,
            origin,
            src,
            obs.bundle("111", attribution="not_detected"),
        )
        await quality_checkpoint(
            m, worker, origin, src, c, lane="helper", helpers=["111"]
        )
    original = m._persistent_error

    async def persistent(*args, **kwargs):
        async with obs.uow(worker, origin) as conn:
            if landing == "source":
                await conn.execute(
                    text(
                        "UPDATE category_post_case_mix SET ratio=0 WHERE source_id=:sid"
                    ),
                    dict(sid=src["source_id"]),
                )
            if landing == "checkpoint":
                await conn.execute(
                    text(
                        "UPDATE media_sources SET sync_checkpoint=jsonb_build_object('changed',true) WHERE id=:sid"
                    ),
                    dict(sid=src["source_id"]),
                )
            if landing == "lease":
                await jobs.finalize_job(conn, job["id"], job["lease_token"], "failed")
        return await original(*args, **kwargs)

    monkeypatch.setattr(m, "_persistent_error", persistent)

    async def response(request):
        if kind == "helper_fallback" and "caption" in request.url.params.get(
            "fields", ""
        ):
            return httpx.Response(
                400,
                json={"error": {"code": 100, "message": "Nonexisting field (caption)"}},
            )
        return httpx.Response(
            400, json={"error": {"code": 190, "message": "private credential payload"}}
        )

    injected, client, requests = await quality_ports(m, c, response)
    try:
        if landing == "lease":
            with pytest.raises(jobs.JobFenced) as fenced:
                await m.run_sync(worker, job, ports=injected)
            result = fenced.value.sync_attempts
        else:
            result = await m.run_sync(worker, job, ports=injected)
        assert (
            result.graph_attempts
            == len(requests)
            == (2 if kind == "helper_fallback" else 1)
        )
        assert result.cdn_attempts == 0 and "private credential payload" not in repr(
            result
        )
        async with obs.uow(worker, origin) as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT state,alerted_at,sync_checkpoint FROM media_sources WHERE id=:sid"
                        ),
                        dict(sid=src["source_id"]),
                    )
                )
                .mappings()
                .one()
            )
            assert row["state"] == ("error" if landing == "error" else "active")
            assert (row["alerted_at"] is not None) == (landing == "error")
            assert "private credential payload" not in json.dumps(
                row["sync_checkpoint"]
            )
    finally:
        await client.aclose()
        await worker.dispose()


async def test_past_posts_sync_port_has_no_background_byte_validation():
    assert "validate" not in service().PastPostsSyncPorts.__dataclass_fields__
