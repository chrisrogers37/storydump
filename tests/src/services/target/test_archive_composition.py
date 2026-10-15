"""The archive composition roots (#1644).

Two things are pinned, and neither needs a database. First, the archive is OFF
by default: with an empty env, or any env that does not both bind this release
and carry a whole key ring, nothing is built and every seam the worker and the
API read stays None, so every archive path keeps failing closed. Second, ARMED,
the composition builds the real graph and hands the capability to every place
that must judge an archive source by it (the claim, the clock, the reaper, a
spent sync's re-arm), the coordinator's `selected_prepare` WRAPPER to the
worker's seam, and the coordinator to the publish pipeline. Providers are
fakes; no request is ever made.

Every record, key and identifier here is synthetic.
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
    mix_conversion,
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
    conversion,
    env_for,
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


def armed(tmp_path, *, sha=WORKER_SHA, **extra) -> dict:
    return {**env_for(tmp_path), composition.RELEASE_VAR: sha, **extra}


def built(tmp_path, client, **extra):
    return composition.build_worker(
        engine=object(),
        env=armed(tmp_path, **extra),
        conversion=conversion(),
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
    "env_of, reasons",
    [
        (lambda tmp: {}, ("no_record", "no_record")),
        # The key ring alone arms nothing; the record alone arms nothing. (The
        # record binds the worker's release, so the API's refuses that first.)
        (lambda tmp: {aa.KEYS_VAR: RING}, ("no_record", "no_record")),
        (
            lambda tmp: {k: v for k, v in armed(tmp).items() if k != aa.KEYS_VAR},
            ("keys_missing", "release_mismatch"),
        ),
        (
            lambda tmp: armed(tmp, sha="e" * 40),
            ("release_mismatch", "release_mismatch"),
        ),
    ],
    ids=["empty env", "keys only", "record only", "another release"],
)
def test_nothing_is_built_unless_both_halves_load_and_the_line_names_why(
    tmp_path, caplog, env_of, reasons
):
    env = env_of(tmp_path)
    with caplog.at_level(logging.INFO, logger=composition.logger.name):
        worker = composition.build_worker(
            engine=object(), env=env, conversion=conversion()
        )
        api = composition.build_api(engine=object(), env=env, conversion=conversion())
    assert (worker, api) == (None, None)
    assert [m for m in caplog.messages if m.startswith("instagram archive")] == [
        f"instagram archive: inactive ({reason})" for reason in reasons
    ]


def test_an_inactive_worker_wires_no_archive_seam_but_still_loads_the_receipt_keys(
    tmp_path,
):
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
    # The key ring loads on its own: a deletion receipt is redacted with it
    # whatever the activation says.
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


def test_an_armed_worker_builds_the_real_graph(tmp_path, client):
    archive = built(tmp_path, client)

    assert archive.capability == ArchiveCapability(enabled=True)
    assert archive.client is client
    assert archive.evidence.run_id == "synthetic-archive-run"
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


async def test_each_port_builds_its_own_adapter_with_the_plans_page_size(
    tmp_path, client
):
    archive = built(tmp_path, client)
    provider = object()

    sync = await archive.sync_ports.prepare_adapter(CAPTURE, provider)
    selected = await archive.preparation.ports.prepare_adapter(CAPTURE, {})

    assert isinstance(sync, InstagramArchiveAdapter)
    assert (sync._page_size, sync._metadata_only) == (25, True)
    assert sync._admission_provider is provider
    assert (selected._page_size, selected._metadata_only) == (25, False)
    assert callable(selected._admission_provider)
    assert sync._base == selected._base == "https://graph.instagram.com/v21.0"


async def test_the_graph_version_is_the_applications_shared_one(tmp_path, client):
    archive = built(tmp_path, client, META_GRAPH_VERSION="v23.0")
    adapter = await archive.sync_ports.prepare_adapter(CAPTURE, object())
    assert adapter._base == "https://graph.instagram.com/v23.0"


def test_an_armed_process_with_a_bad_graph_version_refuses_to_compose(tmp_path, client):
    with pytest.raises(ValueError, match="invalid shared Graph version"):
        built(tmp_path, client, META_GRAPH_VERSION="latest")
    # An inactive process never looks at the value.
    assert (
        composition.build_worker(
            engine=object(),
            env={"META_GRAPH_VERSION": "latest"},
            conversion=None,
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


def test_an_armed_worker_wires_every_seam_to_the_one_graph(tmp_path, client):
    archive = built(tmp_path, client)
    app = composed(archive, env=armed(tmp_path))
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
    tmp_path, client, monkeypatch
):
    archive = built(tmp_path, client)
    seen = {}

    async def run(job, **kwargs):
        seen.update(kwargs)
        return "published"

    monkeypatch.setattr(work_loop.publish_pipeline, "run_publish_pipeline", run)
    registry = composed(archive).registry
    await registry["publish_pipeline"](None, {"id": "j-1"})
    assert seen["archive"] is archive.coordinator


async def test_the_claim_the_reaper_and_the_clock_receive_the_capability(
    tmp_path, client, monkeypatch
):
    archive = built(tmp_path, client)
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
def api_env(tmp_path, monkeypatch):
    monkeypatch.setattr(mix_conversion, "load_evidence", lambda *a, **k: conversion())
    return armed(tmp_path, sha=API_SHA)


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


def test_the_api_arms_only_for_its_own_release(api_env, client):
    # The same record binds the worker's release too, and the API refuses it.
    other = {**api_env, composition.RELEASE_VAR: WORKER_SHA}
    app = app_module.create_app(engine=None, env=other, archive_client=client)
    assert app.state.archive_capability is None and app.state.archive_preview is None
    assert app.state.archive_client is None
    assert app.state.archive_keys == aa.load_keys(other), "receipts keep their keys"


async def test_the_app_closes_the_archive_client_at_shutdown(
    api_env, client, monkeypatch
):
    monkeypatch.setattr(app_module, "_require_key_ring", lambda: None)
    app = app_module.create_app(engine=None, env=api_env, archive_client=client)
    async with app.router.lifespan_context(app):
        assert not client.is_closed
    assert client.is_closed
