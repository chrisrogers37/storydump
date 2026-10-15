"""Real elected discovery, tenant conversion, and lease-last rollback."""

import asyncio
import json
import time

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src import worker
from src.services.target import (
    category_mix,
    jobs,
    mix_conversion,
    scheduler,
    source_state,
)
from src.services.target.work_loop import WorkerConfig
from tests.scripts import test_instagram_sources_gate as foundation
from tests.scripts.conftest import as_user, async_url, set_test_passwords
from tests.scripts.test_instagram_observations_gate import uow, row
from tests.scripts.test_versioned_mix_gate import add, evidence, record

pytestmark = pytest.mark.integration
origin = foundation.origin
archive_engine = foundation.archive_engine


@pytest.fixture
async def converter_engine(lane_db, admin_conn):
    set_test_passwords(admin_conn)
    engine = create_async_engine(async_url(as_user(lane_db, "svc_worker")))
    try:
        yield engine
    finally:
        await engine.dispose()


def composed(engine, tmp_path):
    path = tmp_path / "operator-record.json"
    path.write_text(json.dumps(record()))
    return worker.compose(
        engine=engine,
        config=WorkerConfig(),
        env={
            "SOURCE_MIX_CONVERSION_RECORD": str(path),
            "RAILWAY_GIT_COMMIT_SHA": "b" * 40,
        },
        adapters={},
    )


async def claimed(engine):
    async with engine.connect() as conn:
        job = await jobs.claim_job(
            conn,
            lane="bulk",
            worker="synthetic-converter",
            lease_seconds=60,
            ws_lane_cap=4,
        )
    assert job is not None and job["kind"] == "convert_source_mix"
    return job


async def test_elected_clock_to_registered_converter_atomic_success(
    converter_engine, origin, tmp_path
):
    app = composed(converter_engine, tmp_path)
    async with converter_engine.connect() as elected:
        clock = scheduler.Clock(
            elected,
            converter_engine.connect,
            interval_seconds=15,
            max_inserts=1,
            refresh_cadence_seconds=600,
            recurring={"v": 1},
            conversion_evidence=app.mix_conversion_evidence,
        )
        try:
            counts = await clock.tick_once()
            assert counts["conversion_jobs"] == 1
        finally:
            await clock._election.release()
    bulk = next(loop for loop in app.loops if loop.lane == "bulk")
    assert (
        await bulk.run_once()
        and bulk.processed == 1
        and bulk.failures == bulk.fenced == 0
    )
    async with uow(converter_engine, origin) as conn:
        value = await category_mix.resource(conn, workspace_id=str(origin["ws"]))
        assert value["protocol_version"] == 2 and value["revision"] == 1
        assert (
            await row(conn, "SELECT state FROM jobs WHERE kind='convert_source_mix'")
        )["state"] == "succeeded"
        assert (
            await row(
                conn,
                "SELECT count(*) n FROM audit_events WHERE detail->>'run_id'='synthetic-conversion'",
            )
        )["n"] == 1
    async with converter_engine.begin() as conn:
        assert (
            await mix_conversion.mint_jobs(conn, evidence=app.mix_conversion_evidence)
            == 0
        )
    async with uow(converter_engine, origin) as conn:
        await conn.execute(
            text("UPDATE ig_accounts SET next_slot_at=now() WHERE id=:id"),
            {"id": str(origin["iga"])},
        )
    async with converter_engine.connect() as elected:
        clock = scheduler.Clock(
            elected,
            converter_engine.connect,
            interval_seconds=15,
            max_inserts=1,
            refresh_cadence_seconds=600,
            recurring={"v": 1},
            conversion_evidence=app.mix_conversion_evidence,
        )
        try:
            counts = await clock.tick_once()
            assert counts["slot_jobs"] == 1 and counts["conversion_jobs"] == 0
            assert sum(counts.values()) == 1
        finally:
            await clock._election.release()
    print(
        "MIX_CONVERSION_SAMPLE="
        + json.dumps(
            {
                "typed_record": True,
                "minted": 1,
                "claimed": True,
                "protocol_version": 2,
                "revision": 1,
                "job_state": "succeeded",
                "provider_calls": 0,
            }
        )
    )


async def test_reauth_only_tick_spends_total_max_one_before_conversion(
    converter_engine, origin, tmp_path
):
    app = composed(converter_engine, tmp_path)
    async with uow(converter_engine, origin) as conn:
        await conn.execute(
            text(
                "UPDATE ig_accounts SET state='reauth_required', last_reauth_prompt_at=NULL WHERE id=:id"
            ),
            {"id": str(origin["iga"])},
        )
    async with converter_engine.connect() as elected:
        clock = scheduler.Clock(
            elected,
            converter_engine.connect,
            interval_seconds=15,
            max_inserts=1,
            refresh_cadence_seconds=600,
            recurring={"v": 1},
            conversion_evidence=app.mix_conversion_evidence,
        )
        try:
            counts = await clock.tick_once()
        finally:
            await clock._election.release()
    async with uow(converter_engine, origin) as conn:
        kinds = list(
            (await conn.execute(text("SELECT kind FROM jobs ORDER BY kind"))).scalars()
        )
        assert kinds == ["reauth_prompt"], "reauth consumed the one actual insert slot"
        assert (await row(conn, "SELECT source_mix_revision FROM workspaces"))[
            "source_mix_revision"
        ] is None
    assert counts["reauth_jobs"] == 1 and counts["conversion_jobs"] == 0
    assert sum(counts.values()) == clock.inserts == 1


async def test_same_run_terminal_failure_is_not_reminted_and_new_run_is(
    converter_engine, origin, tmp_path
):
    old = evidence(tmp_path)
    async with converter_engine.begin() as conn:
        assert await mix_conversion.mint_jobs(conn, evidence=old) == 1
    job = await claimed(converter_engine)
    new = evidence(tmp_path, lambda data: data.update(run_id="synthetic-new-run"))
    async with uow(converter_engine, origin) as conn:
        assert (
            await mix_conversion.convert_job(conn, job, evidence=new)
            is jobs.SELF_FINALIZED
        )
    async with uow(converter_engine, origin) as conn:
        assert (await row(conn, "SELECT source_mix_revision FROM workspaces"))[
            "source_mix_revision"
        ] is None
        saved = await row(
            conn, "SELECT state,payload FROM jobs WHERE id=:id", id=str(job["id"])
        )
        assert (
            saved["state"] == "failed"
            and saved["payload"]["conversion_refusal"] == "mix_conversion_run_changed"
        )
    async with converter_engine.begin() as conn:
        assert await mix_conversion.mint_jobs(conn, evidence=old) == 0
        assert await mix_conversion.mint_jobs(conn, evidence=new) == 1


async def test_converter_waiting_on_prefix_cannot_commit_after_lease_expiry(
    converter_engine, origin, tmp_path, sync_conn
):
    trusted = evidence(tmp_path)
    async with converter_engine.begin() as conn:
        assert await mix_conversion.mint_jobs(conn, evidence=trusted) == 1
    job = await claimed(converter_engine)
    with sync_conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET locked_until=clock_timestamp()+interval '1 second' WHERE id=%s",
            (str(job["id"]),),
        )
    sync_conn.commit()
    pid_ready = asyncio.Future()

    async def attempt():
        async with uow(converter_engine, origin) as conn:
            pid_ready.set_result(
                (await conn.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            )
            await mix_conversion.convert_job(conn, job, evidence=trusted)
            await jobs.finalize_job(conn, job["id"], job["lease_token"], "succeeded")

    async with uow(converter_engine, origin) as blocker:
        await source_state.lock_all_workspace_sources(
            blocker, workspace_id=str(origin["ws"])
        )
        task = asyncio.create_task(attempt())
        pid = await pid_ready
        end = time.monotonic() + 5
        while True:
            with sync_conn.cursor() as cur:
                cur.execute(
                    "SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s", (pid,)
                )
                waiting = cur.fetchone()[0] == "Lock"
                cur.execute(
                    "SELECT locked_until<clock_timestamp() FROM jobs WHERE id=%s",
                    (str(job["id"]),),
                )
                expired = cur.fetchone()[0]
            sync_conn.commit()
            if waiting and expired:
                break
            assert time.monotonic() < end, (
                "converter did not wait beyond actual DB lease expiry"
            )
            await asyncio.sleep(0.01)
    with pytest.raises(jobs.JobFenced):
        await task
    async with uow(converter_engine, origin) as conn:
        assert (await row(conn, "SELECT source_mix_revision FROM workspaces"))[
            "source_mix_revision"
        ] is None
        assert (
            await row(
                conn,
                "SELECT count(*) n FROM audit_events WHERE detail->>'run_id'='synthetic-conversion'",
            )
        )["n"] == 0
        assert (
            await row(conn, "SELECT state FROM jobs WHERE id=:id", id=str(job["id"]))
        )["state"] == "leased"


async def test_custom_normalization_roundtrip_retains_legacy_exact_copy(
    archive_engine, origin, tmp_path
):
    async with uow(archive_engine, origin) as conn:
        ids = await add(conn, origin, [".3333", ".3333", ".3333", "0"])
        legacy = await category_mix.convert_workspace(
            conn, workspace_id=str(origin["ws"]), evidence=evidence(tmp_path)
        )
        assert {r["source_id"]: r["ratio"] for r in legacy["rows"]} == {
            **dict.fromkeys(ids[:3], "0.3333"),
            ids[3]: "0",
        }
    async with uow(archive_engine, origin) as conn:
        custom = await category_mix.mutate(
            conn,
            workspace_id=str(origin["ws"]),
            by_user_id=None,
            body={
                "protocol_version": 2,
                "action": "save_custom",
                "expected_revision": 1,
                "rows": [
                    {"source_id": sid, "ratio": ".3333" if i < 3 else "0"}
                    for i, sid in enumerate(ids)
                ],
            },
        )
    async with uow(archive_engine, origin) as conn:
        persisted = await category_mix.resource(conn, workspace_id=str(origin["ws"]))
        assert persisted == custom
        from decimal import Decimal, localcontext

        with localcontext() as context:
            context.prec = 50
            assert sum(Decimal(r["ratio"]) for r in persisted["rows"]) == 1
        assert (
            next(r for r in persisted["rows"] if r["source_id"] == ids[3])["ratio"]
            == "0"
        )


async def test_real_account_removal_allocator_resets_once(
    archive_engine, origin, tmp_path
):
    from src.services.target.source_membership import (
        connect_archive,
        remove_archive_account,
        ArchiveChoice,
    )

    async with uow(archive_engine, origin) as conn:
        await add(conn, origin, ["1"])
        await category_mix.convert_workspace(
            conn, workspace_id=str(origin["ws"]), evidence=evidence(tmp_path)
        )
    async with uow(archive_engine, origin) as conn:
        linked = await connect_archive(
            conn,
            workspace_id=str(origin["ws"]),
            origin_ig_account_id=str(origin["iga"]),
            authorization=foundation.authorization(),
            had_prior_grant=False,
            choice=ArchiveChoice(True, 1),
            allocation_writer=category_mix.allocate_membership,
            capability=foundation.capability(),
        )
    async with uow(archive_engine, origin) as conn:
        removed = await remove_archive_account(
            conn,
            workspace_id=str(origin["ws"]),
            origin_ig_account_id=str(origin["iga"]),
            expected_mix_revision=2,
            allocation_writer=category_mix.allocate_membership,
            capability=foundation.capability(),
        )
        assert removed.mix_revision == 3 and removed.mix_reset_applied
        value = await category_mix.resource(conn, workspace_id=str(origin["ws"]))
        assert (
            value["mode"] == "equal"
            and len(value["rows"]) == 1
            and value["rows"][0]["ratio"] == "1"
        )
        archived = await row(
            conn,
            "SELECT config,source_use_epoch FROM media_sources WHERE id=:id",
            id=linked.source_id,
        )
        assert (
            archived["config"].get("removed") is True
            and archived["source_use_epoch"] > linked.source_use_epoch
        )


async def test_concurrent_converter_and_legacy_writer_share_one_authority(
    archive_engine, origin, tmp_path
):
    async with uow(archive_engine, origin) as conn:
        ids = await add(conn, origin, [".5", ".5"])
    started = asyncio.Event()

    async def convert():
        async with uow(archive_engine, origin) as conn:
            started.set()
            return await category_mix.convert_workspace(
                conn, workspace_id=str(origin["ws"]), evidence=evidence(tmp_path)
            )

    async def old_save():
        async with uow(archive_engine, origin) as conn:
            return await category_mix.set_mix(
                conn,
                workspace_id=str(origin["ws"]),
                mix=[
                    {"source_id": ids[0], "ratio": ".7"},
                    {"source_id": ids[1], "ratio": ".3"},
                ],
                by_user_id=None,
            )

    async with uow(archive_engine, origin) as blocker:
        await source_state.lock_all_workspace_sources(
            blocker, workspace_id=str(origin["ws"])
        )
        first = asyncio.create_task(convert())
        await started.wait()
        second = asyncio.create_task(old_save())
        await asyncio.sleep(0.02)
        assert not first.done() and not second.done()
    converted, saved = await asyncio.gather(first, second, return_exceptions=True)
    assert isinstance(converted, dict)
    assert isinstance(saved, (list, category_mix.MixProtocolRequired))
    async with uow(archive_engine, origin) as conn:
        value = await category_mix.resource(conn, workspace_id=str(origin["ws"]))
        assert value["revision"] == 1 and value["protocol_version"] == 2
        ratios = {r["source_id"]: r["ratio"] for r in value["rows"]}
        assert ratios == (
            {ids[0]: "0.7", ids[1]: "0.3"}
            if isinstance(saved, list)
            else {ids[0]: "0.5", ids[1]: "0.5"}
        )


async def test_concurrent_membership_add_and_custom_save_resolve_fresh_revision(
    archive_engine, origin, tmp_path
):
    async with uow(archive_engine, origin) as conn:
        ids = await add(conn, origin, [".7", ".3"])
        await category_mix.convert_workspace(
            conn, workspace_id=str(origin["ws"]), evidence=evidence(tmp_path)
        )

    async def connect():
        async with uow(archive_engine, origin) as conn:
            return await category_mix.connect_drive(
                conn,
                workspace_id=str(origin["ws"]),
                body={
                    "protocol_version": 2,
                    "expected_revision": 1,
                    "folder_ref": "synthetic-concurrent",
                    "enabled": True,
                },
            )

    async def save():
        async with uow(archive_engine, origin) as conn:
            return await category_mix.mutate(
                conn,
                workspace_id=str(origin["ws"]),
                body={
                    "protocol_version": 2,
                    "action": "save_custom",
                    "expected_revision": 1,
                    "rows": [
                        {"source_id": ids[0], "ratio": ".6"},
                        {"source_id": ids[1], "ratio": ".4"},
                    ],
                },
                by_user_id=None,
            )

    async with uow(archive_engine, origin) as blocker:
        await source_state.lock_all_workspace_sources(
            blocker, workspace_id=str(origin["ws"])
        )
        tasks = [asyncio.create_task(connect()), asyncio.create_task(save())]
        await asyncio.sleep(0.02)
        assert not any(task.done() for task in tasks)
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    assert sum(isinstance(o, category_mix.MixConflict) for o in outcomes) == 1
    assert sum(isinstance(o, dict) for o in outcomes) == 1
    async with uow(archive_engine, origin) as conn:
        value = await category_mix.resource(conn, workspace_id=str(origin["ws"]))
        assert value["revision"] == 2
        assert len(value["rows"]) == (3 if isinstance(outcomes[0], dict) else 2)
