"""Registry dispatch and tenant-bound source lookup against the real ledger.

The production provider CHECK is deliberately unchanged. This fixture admits
only a test provider to prove the registry before any new product is enabled.
"""

import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src import worker
from src.channels.telegram_transport import MediaUnavailable
from src.services.target import (
    jobs,
    media_adapters,
    media_sync,
    publish_pipeline,
    unit_of_work,
)
from src.services.target.work_loop import WorkerConfig
from tests.scripts.conftest import (
    as_user,
    async_url,
    seed_workspace_chain,
    set_test_passwords,
)
from tests.scripts.test_w6_sync_gate import (
    _bind,
    _notifications,
    _item,
    _items,
    _jobs,
    _source_row,
)

pytestmark = [pytest.mark.integration]


@pytest.fixture
def registry_world(world_db, sync_conn):
    chain = seed_workspace_chain(sync_conn, "registry")
    with sync_conn.cursor() as cur:
        cur.execute("ALTER TABLE media_sources DROP CONSTRAINT ck_sources_provider")
        cur.execute(
            "ALTER TABLE media_sources ADD CONSTRAINT ck_sources_provider"
            " CHECK (provider IN ('gdrive', 'fake_past_posts'))"
        )
        cur.execute(
            "UPDATE media_sources SET provider = 'fake_past_posts', config = %s::jsonb"
            " WHERE id = %s",
            (json.dumps({"v": 9, "bucket": "past-posts-a"}), chain["src"]),
        )
    sync_conn.commit()
    return chain


@pytest.fixture(params=("owner", "svc_worker"))
def registry_actor(request, world_db, admin_conn):
    if request.param == "owner":
        return world_db, None
    set_test_passwords(admin_conn)
    return as_user(world_db, "svc_worker"), "svc_worker"


async def _enqueue_registry_sync(engine, chain):
    # The clock correctly refuses fixture-only providers. These tests exercise
    # registry routing and failures through a real queued job and worker lease.
    workspace, source = str(chain["ws"]), str(chain["src"])
    async with unit_of_work.unit_of_work(
        engine, workspace, actor_kind="worker"
    ).begin() as session:
        job_id = await jobs.enqueue(
            session,
            kind="sync_media_source",
            workspace_id=workspace,
            serialization_key=f"src:{source}",
            payload={"v": 1, "source_id": source, "reason": "baseline"},
            max_attempts=5,
            deadline_seconds=6 * 60 * 60,
        )
        assert job_id is not None


async def _assert_worker_role(engine, expected):
    if expected is None:
        return
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT current_user"))).scalar() == expected
        assert (
            await conn.execute(text("SELECT count(*) FROM media_sources"))
        ).scalar() == 0, "the worker without a tenant must see no source rows"


class PastPostsAdapter:
    """No Drive keys; exhaustion alone never means a whole inventory."""

    def __init__(self, engine):
        self.engine = engine
        self.calls = []
        self.fetches = []

    @staticmethod
    def initial_checkpoint():
        return {"offset": 0}

    @staticmethod
    def checkpoint_incomplete(checkpoint):
        return bool(checkpoint and checkpoint.get("has_more"))

    @staticmethod
    def checkpoint_whole(checkpoint):
        return False

    async def list_changes(self, config, checkpoint, *, source_id, workspace_id):
        assert self.engine.pool.checkedout() == 0, "no SQL connection during listing"
        self.calls.append((config, checkpoint, source_id, workspace_id))
        offset = checkpoint["offset"]
        return [_item(f"past-posts-{offset}")], {
            "offset": offset + 1,
            "has_more": offset == 0,
        }

    async def fetch_bytes(self, config, file_ref, **kwargs):
        assert self.engine.pool.checkedout() == 0, "no SQL connection during fetch"
        self.fetches.append((config, file_ref, kwargs))
        return b"past_posts", "f.jpg", "image/jpeg"


async def test_non_drive_checkpoint_resumes_two_chunks_without_tombstoning(
    world_db, sync_conn, registry_world, registry_actor
):
    chain = registry_world
    dsn, expected_role = registry_actor
    engine = create_async_engine(async_url(dsn))
    adapter = PastPostsAdapter(engine)
    deps = SimpleNamespace(
        engine=engine, adapters={"gdrive": object(), "fake_past_posts": adapter}
    )
    source, workspace = str(chain["src"]), str(chain["ws"])
    job = {"id": "initial", "workspace_id": workspace, "payload": {"source_id": source}}
    try:
        await _assert_worker_role(engine, expected_role)
        other = seed_workspace_chain(sync_conn, "registry-sync-other")
        assert (
            await media_sync.sync_media_source(
                deps,
                None,
                {
                    **job,
                    "workspace_id": str(other["ws"]),
                },
            )
            == "missing"
        )
        assert adapter.calls == []
        assert await media_sync.sync_media_source(deps, None, job) == "chained"
        cp = _source_row(sync_conn, chain["src"])["sync_checkpoint"]
        assert cp["offset"] == 1 and cp["has_more"] is True
        assert "v" not in cp and "current" not in cp and "page_token" not in cp
        chunks = _jobs(sync_conn, "first_ingest_chunk")
        assert len(chunks) == 1 and chunks[0]["payload"]["walk"] == cp["walk"]
        assert (
            await media_sync.first_ingest_chunk(
                deps,
                None,
                {
                    "id": "chunk",
                    "workspace_id": workspace,
                    "payload": chunks[0]["payload"],
                },
            )
            == "synced"
        )
        assert adapter.calls[1][1] == cp
        source_row = _source_row(sync_conn, chain["src"])
        assert source_row["sync_checkpoint"]["offset"] == 2
        assert source_row["next_sync_at"] is not None
        assert source_row["last_sync_success_at"] is not None
        assert len(_jobs(sync_conn, "first_ingest_chunk")) == 1
        assert {r[0] for r in _items(sync_conn, chain["ws"])} == {
            "ref-registry",
            "past-posts-0",
            "past-posts-1",
        }
        assert all(r[2] == "available" for r in _items(sync_conn, chain["ws"]))
        assert all(
            call[0] == {"v": 9, "bucket": "past-posts-a"} for call in adapter.calls
        )
        assert all(call[2:] == (source, workspace) for call in adapter.calls)
    finally:
        await engine.dispose()


async def test_unknown_provider_retries_job_without_marking_source_error(
    world_db, sync_conn, registry_world, caplog
):
    chain = registry_world
    before = datetime.now(timezone.utc)
    engine = create_async_engine(async_url(world_db))
    try:
        await _enqueue_registry_sync(engine, chain)
        app = worker.compose(
            engine=engine, config=WorkerConfig(), env={}, adapters={"gdrive": object()}
        )
        loop = next(loop for loop in app.loops if loop.lane == "bulk")
        async with engine.connect() as conn:
            loop.bind_claim_conn(conn)
            assert await loop.run_once()
        source = _source_row(sync_conn, chain["src"])
        assert source["state"] == "active" and source["alerted_at"] is None
        assert source["sync_checkpoint"] is None
        job = _jobs(sync_conn, "sync_media_source")[0]
        assert job["state"] == "ready" and job["run_at"] > before
        with sync_conn.cursor() as cur:
            cur.execute("SELECT attempts FROM jobs WHERE id = %s", (job["id"],))
            assert cur.fetchone()[0] == 1
        assert "ProviderNotWired" in caplog.text
    finally:
        await engine.dispose()


async def test_old_card_ids_resolve_config_only_in_the_named_workspace(
    world_db, sync_conn, registry_world, registry_actor
):
    chain = registry_world
    other = seed_workspace_chain(sync_conn, "registry-other")
    dsn, expected_role = registry_actor
    engine = create_async_engine(async_url(dsn))
    adapter = PastPostsAdapter(engine)
    fetch = worker._card_media_fetch(
        engine, {"gdrive": object(), "fake_past_posts": adapter}
    )
    media = {
        "source_id": str(chain["src"]),
        "workspace_id": str(other["ws"]),
        "ref": "opaque",
        "kind": "image",
    }
    try:
        await _assert_worker_role(engine, expected_role)
        # Both under RLS and with the owner bypassing it, identity must match.
        with pytest.raises(MediaUnavailable):
            await fetch(media)
        assert adapter.fetches == []
        media["workspace_id"] = str(chain["ws"])
        assert await fetch(media) == (b"past_posts", "f.jpg", "image/jpeg")
        assert adapter.fetches[0][:2] == ({"v": 9, "bucket": "past-posts-a"}, "opaque")
        with sync_conn.cursor() as cur:
            cur.execute(
                "UPDATE media_sources SET state = 'paused' WHERE id = %s",
                (chain["src"],),
            )
        sync_conn.commit()
        with pytest.raises(MediaUnavailable):
            await fetch(media)
        assert len(adapter.fetches) == 1
    finally:
        await engine.dispose()


async def test_equal_file_refs_from_different_providers_cannot_move_a_live_row(
    world_db, sync_conn, registry_world
):
    from src.services.target.google_drive_adapter import GoogleDriveAdapter
    from tests.scripts.conftest import seed_intent_chain

    chain = registry_world
    with sync_conn.cursor() as cur:
        owner = seed_intent_chain(cur, chain["ws"], "drive-owner")
        cur.execute(
            "UPDATE media_items SET provider_file_ref = 'past-posts-0',"
            " content_hash = 'hash-past-posts-0', created_at = now() - interval '1 day'"
            " WHERE id = %s",
            (owner["media"],),
        )
        cur.execute(
            "UPDATE media_sources SET sync_checkpoint = jsonb_build_object("
            " 'v', 2, 'whole', true, 'started_at', now()::text) WHERE id = %s",
            (owner["src"],),
        )
    sync_conn.commit()
    engine = create_async_engine(async_url(world_db))
    adapter = PastPostsAdapter(engine)
    deps = SimpleNamespace(
        engine=engine,
        adapters={"gdrive": GoogleDriveAdapter, "fake_past_posts": adapter},
    )
    try:
        assert (
            await media_sync.sync_media_source(
                deps,
                None,
                {
                    "id": "collision",
                    "workspace_id": str(chain["ws"]),
                    "payload": {"source_id": str(chain["src"])},
                },
            )
            == "chained"
        )
        with sync_conn.cursor() as cur:
            cur.execute(
                "SELECT source_id FROM media_items WHERE id = %s", (owner["media"],)
            )
            assert cur.fetchone()[0] == owner["src"]
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "error_type, recovery",
    [
        (media_adapters.MediaSourceGone, "Restore access or reconnect"),
        (media_adapters.MediaCredentialDead, "Reconnect the source"),
    ],
)
async def test_persistent_errors_never_copy_provider_secrets_to_alerts_or_logs(
    world_db, sync_conn, registry_world, caplog, error_type, recovery
):
    chain = registry_world
    binding = _bind(sync_conn, chain["ws"])
    secret = "https://provider.invalid/media?access_token=private-provider-secret"
    engine = create_async_engine(async_url(world_db))

    class BrokenAdapter(PastPostsAdapter):
        async def list_changes(self, *args, **kwargs):
            raise error_type(secret)

    deps = SimpleNamespace(
        engine=engine, adapters={"fake_past_posts": BrokenAdapter(engine)}
    )
    try:
        assert (
            await media_sync.sync_media_source(
                deps,
                None,
                {
                    "id": "persistent-error",
                    "workspace_id": str(chain["ws"]),
                    "payload": {"source_id": str(chain["src"])},
                },
            )
            == "source-error"
        )
        assert _source_row(sync_conn, chain["src"])["state"] == "error"
        notices = _notifications(sync_conn, binding)
        assert len(notices) == 1
        persisted = json.dumps(notices)
        assert recovery in persisted
        assert "private-provider-secret" not in persisted
        assert "access_token" not in persisted
        assert "provider.invalid" not in persisted
        assert error_type.__name__ in caplog.text
        assert "private-provider-secret" not in caplog.text
        assert "provider.invalid" not in caplog.text
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "error_type", [media_adapters.MediaRetryableError, media_adapters.MediaLostResponse]
)
async def test_retryable_provider_errors_do_not_leak_through_work_loop_logs(
    world_db, sync_conn, registry_world, caplog, error_type
):
    chain = registry_world
    engine = create_async_engine(async_url(world_db))
    secret = "https://provider.invalid/media?access_token=private-provider-secret"

    class BrokenAdapter(PastPostsAdapter):
        async def list_changes(self, *args, **kwargs):
            raise error_type(secret)

    try:
        await _enqueue_registry_sync(engine, chain)
        app = worker.compose(
            engine=engine,
            config=WorkerConfig(),
            env={},
            adapters={"fake_past_posts": BrokenAdapter(engine)},
        )
        loop = next(loop for loop in app.loops if loop.lane == "bulk")
        async with engine.connect() as conn:
            loop.bind_claim_conn(conn)
            assert await loop.run_once()
        assert _jobs(sync_conn, "sync_media_source")[0]["state"] == "ready"
        assert _source_row(sync_conn, chain["src"])["state"] == "active"
        assert error_type.__name__ in caplog.text
        assert "private-provider-secret" not in caplog.text
        assert "provider.invalid" not in caplog.text
        assert "access_token" not in caplog.text
    finally:
        await engine.dispose()


async def test_publish_source_join_reads_only_its_tenant_as_worker(
    world_db, sync_conn, registry_world, admin_conn
):
    chain = registry_world
    other = seed_workspace_chain(sync_conn, "registry-publish-other")
    lease = str(uuid.uuid4())
    with sync_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO jobs (workspace_id, kind, lane, serialization_key, payload,"
            " max_attempts, attempts, state, lease_token, locked_by, locked_until)"
            " VALUES (%s, 'publish_pipeline', 'bulk', 'ig:registry', %s::jsonb,"
            " 5, 1, 'leased', %s, 'registry-test', now() + interval '5 minutes') RETURNING id",
            (
                chain["ws"],
                json.dumps({"v": 1, "intent_id": str(chain["intent"])}),
                lease,
            ),
        )
        job_id = cur.fetchone()[0]
    sync_conn.commit()
    set_test_passwords(admin_conn)
    engine = create_async_engine(async_url(as_user(world_db, "svc_worker")))
    job = {
        "id": str(job_id),
        "lease_token": lease,
        "workspace_id": str(chain["ws"]),
        "payload": {"intent_id": str(chain["intent"])},
    }
    uow = unit_of_work.unit_of_work(engine, str(chain["ws"]), actor_kind="system")
    try:
        await _assert_worker_role(engine, "svc_worker")
        loaded = await publish_pipeline._load(uow, job)
        assert loaded.intent["provider"] == "fake_past_posts"
        assert loaded.intent["source_config"] == {"v": 9, "bucket": "past-posts-a"}
        assert str(loaded.intent["source_id"]) == str(chain["src"])
        # A leased job in A cannot read B's intent/source/config through _load.
        foreign = await publish_pipeline._load(
            uow,
            {
                **job,
                "payload": {"intent_id": str(other["intent"])},
            },
        )
        assert foreign is None
    finally:
        await engine.dispose()


@pytest.mark.parametrize("failure_at", ["persist_source_error", "reschedule_job"])
async def test_a_nested_database_failure_cannot_log_the_original_provider_secret(
    world_db, sync_conn, registry_world, monkeypatch, caplog, failure_at
):
    from sqlalchemy.ext.asyncio import AsyncSession

    chain = registry_world
    engine = create_async_engine(async_url(world_db))
    secret = "https://provider.invalid/media?access_token=private-provider-secret"
    fault_type = (
        media_adapters.MediaSourceGone
        if failure_at == "persist_source_error"
        else media_adapters.MediaRetryableError
    )

    class BrokenAdapter(PastPostsAdapter):
        async def list_changes(self, *args, **kwargs):
            raise fault_type(secret)

    if failure_at == "persist_source_error":
        original_execute = AsyncSession.execute

        async def broken_write(session, statement, *args, **kwargs):
            if "UPDATE media_sources SET state = 'error'" in str(statement):
                raise RuntimeError("local database unavailable")
            return await original_execute(session, statement, *args, **kwargs)

        monkeypatch.setattr(AsyncSession, "execute", broken_write)
    else:

        async def broken_reschedule(*args, **kwargs):
            raise RuntimeError("local database unavailable")

        monkeypatch.setattr(jobs, "reschedule_job", broken_reschedule)

    try:
        await _enqueue_registry_sync(engine, chain)
        app = worker.compose(
            engine=engine,
            config=WorkerConfig(),
            env={},
            adapters={"fake_past_posts": BrokenAdapter(engine)},
        )
        loop = next(loop for loop in app.loops if loop.lane == "bulk")
        async with engine.connect() as conn:
            loop.bind_claim_conn(conn)
            assert await loop.run_once()
        assert "local database unavailable" in caplog.text
        assert "private-provider-secret" not in caplog.text
        assert "provider.invalid" not in caplog.text
        assert "access_token" not in caplog.text
        assert _source_row(sync_conn, chain["src"])["state"] == "active"
    finally:
        await engine.dispose()
