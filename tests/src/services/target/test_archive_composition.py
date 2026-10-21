"""The archive composition roots (#1644).

Two things are pinned, and neither needs a database. First, the archive is off
without its key ring: with an empty env, or a ring that is missing or malformed,
nothing is built and every seam the worker and the API read stays None, so every
archive path keeps failing closed. Second, with the ring, on any release and with
no record or switch, the composition builds the real graph and hands the capability to every place
that must judge an archive source by it (the claim, the clock, the reaper, a
spent sync's re-arm), the coordinator's `selected_prepare` WRAPPER to the
worker's seam, and the coordinator to the publish pipeline. Providers are
fakes; no request is ever made.

Every key and identifier here is synthetic.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from src.api import app as app_module
from src.services.target import archive_activation as aa
from src.services.target import archive_composition as composition
from src.services.target import (
    jobs,
    scheduler,
    unit_of_work,
    work_loop,
)
from src.services.target import source_read_admission as admission
from src.services.target.archive_preview import ArchivePreview
from src.services.target.instagram_archive_adapter import InstagramArchiveAdapter
from src.services.target.instagram_publish import ArchivePipeline
from src.services.target.instagram_sources import ArchiveCapability
from src.services.target.media_adapters import MediaSourceFenceDrop
from src.services.target.work_loop import Parked, WorkerConfig
from src.worker import _clock_for, compose
from tests.src.services.target.test_archive_activation import (
    API_SHA,
    RING,
    WORKER_SHA,
)

CAPTURE = admission.SourceReadCapture(
    "11111111-1111-4111-8111-111111111111",
    "22222222-2222-4222-8222-222222222222",
    "instagram_archive",
    3,
    "33333333-3333-4333-8333-333333333333",
    "44444444-4444-4444-8444-444444444444",
    "synthetic-account",
)


def refuse(request):
    raise AssertionError("the composition made a request")


@pytest.fixture
def client():
    return httpx.AsyncClient(transport=httpx.MockTransport(refuse))


def armed(*, sha=WORKER_SHA, **extra) -> dict:
    return {aa.KEYS_VAR: RING, composition.RELEASE_VAR: sha, **extra}


def built(client, **extra):
    return composition.build_worker(
        engine=object(),
        env=armed(**extra),
        client=client,
    )


def composed(archive, env=None, **seams):
    """The worker's graph over fakes; `adapters` makes the publish kind live."""
    return compose(
        engine=object(),
        config=WorkerConfig(),
        env=env if env is not None else {},
        adapters={"gdrive": object()},
        meta=object(),
        transit=object(),
        archive=archive,
        **seams,
    )


# --- default off ----------------------------------------------------------------


@pytest.mark.parametrize(
    "env, reason",
    [
        ({}, "keys_missing"),
        ({composition.RELEASE_VAR: WORKER_SHA}, "keys_missing"),
        ({aa.KEYS_VAR: "  "}, "keys_missing"),
        ({aa.KEYS_VAR: "1:short", composition.RELEASE_VAR: WORKER_SHA}, "keys_invalid"),
    ],
    ids=["empty env", "a release alone", "a blank ring", "a malformed ring"],
)
def test_nothing_is_built_without_the_key_ring_and_the_line_names_why(
    caplog, env, reason
):
    with caplog.at_level(logging.INFO, logger=composition.logger.name):
        worker = composition.build_worker(engine=object(), env=env)
        api = composition.build_api(engine=object(), env=env)
    assert (worker, api) == (None, None)
    assert [m for m in caplog.messages if m.startswith("instagram archive")] == [
        f"instagram archive: inactive ({reason})"
    ] * 2


@pytest.mark.parametrize("release", [WORKER_SHA, API_SHA, None])
def test_the_ring_arms_on_any_release_with_nothing_else_set(caplog, client, release):
    env = {aa.KEYS_VAR: RING}
    if release is not None:
        env[composition.RELEASE_VAR] = release
    with caplog.at_level(logging.INFO, logger=composition.logger.name):
        worker = composition.build_worker(engine=object(), env=env, client=client)
    run = aa.release_evidence(release).run_id
    assert worker is not None and worker.evidence == aa.release_evidence(release)
    assert f"instagram archive: armed (run={run})" in caplog.messages


def test_an_inactive_worker_wires_no_archive_seam_but_still_loads_the_receipt_keys():
    bare = composed(None)
    deps = bare.deps
    assert (
        deps.archive,
        deps.archive_sync,
        deps.selected_prepare,
        deps.archive_capability,
        deps.archive_evidence,
        deps.archive_client,
        deps.archive_keys,
    ) == (None,) * 7
    assert all(loop._archive_capability is None for loop in bare.loops)
    assert isinstance(bare.registry["reconcile_archive_accounts"], Parked)
    # `compose` reads the key ring itself: a deletion receipt is redacted with
    # it whatever archive `main` built.
    ring = composed(None, env={aa.KEYS_VAR: RING}).deps
    assert ring.archive_keys == aa.load_keys({aa.KEYS_VAR: RING})
    assert ring.archive_keys is not None and ring.archive_capability is None


async def test_the_inactive_publish_executor_hands_the_pipeline_no_archive(
    monkeypatch,
):
    seen = {}

    async def run(job, **kwargs):
        seen.update(kwargs)
        return "published"

    monkeypatch.setattr(work_loop.publish_pipeline, "run_publish_pipeline", run)
    registry = composed(None).registry
    assert not isinstance(registry["publish_pipeline"], Parked)
    assert (
        await registry["publish_pipeline"](None, {"id": "j-1"}) is jobs.SELF_FINALIZED
    )
    assert seen["archive"] is None


def test_an_inactive_app_has_no_archive_seam():
    app = app_module.create_app(engine=None, env={})
    state = app.state
    assert (
        state.archive_capability,
        state.archive_preview,
        state.archive_keys,
        state.archive_client,
    ) == (None,) * 4


# --- armed: the graph -------------------------------------------------------------


def test_an_armed_worker_builds_the_real_graph(client):
    archive = built(client)

    assert archive.capability == ArchiveCapability(enabled=True)
    assert archive.client is client
    assert archive.evidence == aa.release_evidence(WORKER_SHA)
    assert archive.keys.current_version == 2
    coordinator = archive.coordinator
    assert type(coordinator) is ArchivePipeline
    assert coordinator.capability is archive.capability
    # The coordinator is built over the preparation it wraps, not beside it.
    assert coordinator.fetch_selected == archive.preparation.fetch_selected
    assert coordinator.prepare_selected == archive.preparation.selected_prepare
    assert archive.preparation.ports.capability is archive.capability
    assert archive.preparation.ports.tombstone_keys is archive.keys
    assert archive.sync_ports.capability is archive.capability
    assert archive.sync_ports.tombstone_keys is archive.keys


async def test_each_port_builds_its_own_adapter_with_the_plans_page_size(client):
    archive = built(client)
    provider = object()

    sync = await archive.sync_ports.prepare_adapter(CAPTURE, provider)
    selected = await archive.preparation.ports.prepare_adapter(CAPTURE, {})

    assert isinstance(sync, InstagramArchiveAdapter)
    assert (sync._page_size, sync._metadata_only) == (25, True)
    assert sync._admission_provider is provider
    assert (selected._page_size, selected._metadata_only) == (25, False)
    assert callable(selected._admission_provider)
    assert sync._base == selected._base == "https://graph.instagram.com/v21.0"


async def test_the_graph_version_is_the_applications_shared_one(client):
    archive = built(client, META_GRAPH_VERSION="v23.0")
    adapter = await archive.sync_ports.prepare_adapter(CAPTURE, object())
    assert adapter._base == "https://graph.instagram.com/v23.0"


def test_an_armed_process_with_a_bad_graph_version_refuses_to_compose(client):
    with pytest.raises(ValueError, match="invalid shared Graph version"):
        built(client, META_GRAPH_VERSION="latest")
    # An inactive process never looks at the value.
    assert (
        composition.build_worker(
            engine=object(),
            env={"META_GRAPH_VERSION": "latest"},
        )
        is None
    )


async def test_the_access_provider_refuses_a_call_for_another_source_before_any_read():
    access = composition.access_provider(
        None, capture=CAPTURE, capability=ArchiveCapability(enabled=True)
    )
    for source, workspace in (
        ("another-source", CAPTURE.workspace_id),
        (CAPTURE.source_id, "another-workspace"),
    ):
        with pytest.raises(MediaSourceFenceDrop) as refused:
            await access(source, workspace_id=workspace)
        assert refused.value.reason == "source_capture_mismatch"


# --- armed: the worker's wiring ---------------------------------------------------


def test_an_armed_worker_wires_every_seam_to_the_one_graph(client):
    archive = built(client)
    app = composed(archive, env=armed())
    deps = app.deps

    assert deps.archive is archive.coordinator
    # The WRAPPER, which owns the bound-content review route: never the bare
    # `SelectedPreparation` method the coordinator was built over.
    assert deps.selected_prepare == archive.coordinator.selected_prepare
    assert deps.selected_prepare != archive.preparation.selected_prepare
    assert deps.archive_sync is archive.sync_ports
    assert deps.archive_capability is archive.capability
    assert deps.archive_evidence is archive.evidence
    assert deps.archive_client is client
    assert deps.archive_keys == archive.keys
    assert app.loops and all(
        loop._archive_capability is archive.capability for loop in app.loops
    )
    live = {k for k, e in app.registry.items() if not isinstance(e, Parked)}
    # The reconciliation is live because the capability AND the adapter's client
    # (the one handed to `built`) reached the worker's deps.
    assert {
        "archive_lifecycle_sweep",
        "meta_deletion_cleanup",
        "reconcile_archive_accounts",
    } <= live
    assert app.recurring["archive_lifecycle_sweep"] == 60.0
    assert set(app.recurring) - {"v"} <= live


async def test_the_publish_executor_hands_the_pipeline_the_coordinator(
    client, monkeypatch
):
    archive = built(client)
    seen = {}

    async def run(job, **kwargs):
        seen.update(kwargs)
        return "published"

    monkeypatch.setattr(work_loop.publish_pipeline, "run_publish_pipeline", run)
    registry = composed(archive).registry
    await registry["publish_pipeline"](None, {"id": "j-1"})
    assert seen["archive"] is archive.coordinator


async def test_the_claim_the_reaper_and_the_clock_receive_the_capability(
    client, monkeypatch
):
    archive = built(client)
    app = composed(archive)
    claimed, reaped, ticked = {}, {}, {}

    async def claim_job(conn, **kwargs):
        claimed.update(kwargs)

    async def execute_reap_expired(engine, **kwargs):
        reaped.update(kwargs)

    async def tick(session, **kwargs):
        ticked.update(kwargs)
        return {"slot_jobs": 0}

    monkeypatch.setattr(work_loop.jobs, "claim_job", claim_job)
    monkeypatch.setattr(
        work_loop.scheduler, "execute_reap_expired", execute_reap_expired
    )
    monkeypatch.setattr(scheduler, "tick", tick)

    loop = app.loops[0]
    loop.bind_claim_conn(object())
    await loop._claim()
    await app.registry["reap_expired"](None, {"id": "j-reap"})
    clock = _clock_for(app, object())
    clock._election = SimpleNamespace(try_acquire=_always)
    clock._session_factory = _session_factory
    await clock.tick_once()

    assert claimed["capability"] is archive.capability
    assert reaped["capability"] is archive.capability
    assert ticked["capability"] is archive.capability


async def _always():
    return True


@asynccontextmanager
async def _session_factory():
    yield SimpleNamespace(commit=_commit)


async def _commit():
    return None


async def test_a_default_claim_and_clock_carry_no_capability(monkeypatch):
    app = composed(None)
    claimed, ticked = {}, {}

    async def claim_job(conn, **kwargs):
        claimed.update(kwargs)

    async def tick(session, **kwargs):
        ticked.update(kwargs)
        return {"slot_jobs": 0}

    monkeypatch.setattr(work_loop.jobs, "claim_job", claim_job)
    monkeypatch.setattr(scheduler, "tick", tick)
    loop = app.loops[0]
    loop.bind_claim_conn(object())
    await loop._claim()
    clock = _clock_for(app, object())
    clock._election = SimpleNamespace(try_acquire=_always)
    clock._session_factory = _session_factory
    await clock.tick_once()

    assert claimed["capability"] is None and ticked["capability"] is None


async def test_a_spent_job_is_settled_under_the_loops_capability(monkeypatch):
    cap = ArchiveCapability(enabled=True)
    seen = {}

    async def settle(session, job, *, capability=None):
        seen["capability"] = capability

    async def notify(session, job, parked):
        return None

    async def finalize(session, job_id, token, state):
        seen["state"] = state

    class Session:
        @asynccontextmanager
        async def begin_nested(self):
            yield

    @asynccontextmanager
    async def session_for(job):
        yield Session()

    async def explode(session, job):
        raise RuntimeError("boom")

    monkeypatch.setattr(work_loop, "_settle_exhausted", settle)
    monkeypatch.setattr(work_loop, "_notify_exhausted", notify)
    monkeypatch.setattr(work_loop.jobs, "finalize_job", finalize)
    job = {
        "id": "j-1",
        "kind": "k",
        "lease_token": "t",
        "attempts": 5,
        "max_attempts": 5,
        "lane": "bulk",
    }
    for capability in (cap, None):
        seen.clear()
        loop = work_loop.WorkLoop(
            session_for=session_for,
            lane="bulk",
            registry={"k": explode},
            heartbeat=None,
            config=WorkerConfig(),
            worker_name="w",
            archive_capability=capability,
        )
        await loop._run_job(job)
        assert seen == {"capability": capability, "state": "failed"}


# --- the reaper's door ------------------------------------------------------------


class _Door:
    """A session that records each statement and answers every read empty."""

    def __init__(self, log):
        self.log = log

    async def execute(self, statement, params=None):
        self.log.append((str(statement), dict(params or {})))
        return SimpleNamespace(scalar=lambda: 0, all=lambda: [])

    async def commit(self):
        return None


@pytest.mark.parametrize("enabled", [True, False])
async def test_the_reaper_runs_the_four_argument_door_only_when_activated(
    monkeypatch, enabled
):
    log = []

    @asynccontextmanager
    async def system():
        yield _Door(log)

    async def settled(session, *, limit):
        return None

    monkeypatch.setattr(
        unit_of_work, "poller_session_factory", lambda engine, tenant: system
    )
    monkeypatch.setattr(scheduler.prompts, "sweep_settled_cards", settled)

    await scheduler.execute_reap_expired(
        object(),
        limit=5,
        approval_ttl_seconds=60,
        approved_ttl_seconds=120,
        capability=ArchiveCapability(enabled=True) if enabled else None,
    )

    sql, params = log[0]
    assert "fn_reaper_sweep" in sql
    assert ("CAST(:activated AS boolean)" in sql) is enabled
    assert params == {
        "lim": 5,
        "a": 60,
        "b": 120,
        **({"activated": True} if enabled else {}),
    }


# --- the API ----------------------------------------------------------------------


@pytest.fixture
def api_env():
    return armed(sha=API_SHA)


def test_an_armed_api_builds_the_preview_and_hands_the_routes_the_capability(
    api_env, client
):
    app = app_module.create_app(engine=None, env=api_env, archive_client=client)
    state = app.state

    assert state.archive_capability == ArchiveCapability(enabled=True)
    assert type(state.archive_preview) is ArchivePreview
    assert state.archive_preview.ports.capability is state.archive_capability
    assert state.archive_keys == aa.load_keys(api_env)
    assert state.archive_client is client


def test_the_api_arms_on_any_release_and_never_without_the_ring(api_env, client):
    other = {**api_env, composition.RELEASE_VAR: WORKER_SHA}
    app = app_module.create_app(engine=None, env=other, archive_client=client)
    assert app.state.archive_capability == ArchiveCapability(enabled=True)
    bare = {k: v for k, v in api_env.items() if k != aa.KEYS_VAR}
    app = app_module.create_app(engine=None, env=bare, archive_client=client)
    assert app.state.archive_capability is None and app.state.archive_preview is None
    assert (app.state.archive_client, app.state.archive_keys) == (None, None)


async def test_the_app_closes_the_archive_client_at_shutdown(
    api_env, client, monkeypatch
):
    monkeypatch.setattr(app_module, "_require_key_ring", lambda: None)
    app = app_module.create_app(engine=None, env=api_env, archive_client=client)
    async with app.router.lifespan_context(app):
        assert not client.is_closed
    assert client.is_closed
