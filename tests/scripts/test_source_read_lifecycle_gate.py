"""Real job-lock ordering, reaper fences, fresh expiry and grant boundaries."""

import asyncio
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from src.services.target import jobs, work_loop
from tests.scripts import test_source_read_admission_gate as admission
from tests.scripts import test_instagram_sources_gate as foundation
from tests.scripts import test_instagram_observations_gate as obs
from tests.scripts.conftest import async_url, as_user, seed_workspace_chain

pytestmark = [pytest.mark.integration]
archive_engine = foundation.archive_engine
origin = foundation.origin


async def test_old_generation_deletion_and_policy_exclusions_do_not_block_read(
    archive_engine, origin
):
    src = await admission.ready(archive_engine, origin)
    fresh = await foundation.ensure(archive_engine, origin, foundation.authorization())
    async with obs.uow(archive_engine, origin) as conn:
        await conn.execute(
            text(
                "UPDATE instagram_archive_generations SET lifecycle='deletion_pending' WHERE id=:gid"
            ),
            dict(gid=src["generation_id"]),
        )
        await conn.execute(
            text(
                "UPDATE instagram_source_policies SET minimum_age_days=36500,include_credited=false,include_unknown=false WHERE source_id=:sid"
            ),
            dict(sid=src["source_id"]),
        )
    c = await admission.capture(archive_engine, origin, src)
    assert c.generation_id == fresh["generation_id"]


async def test_expiry_uses_wall_time_even_in_existing_transaction(
    archive_engine, origin
):
    src = await admission.ready(archive_engine, origin)
    c = await admission.capture(archive_engine, origin, src)
    async with obs.uow(archive_engine, origin) as conn:
        await conn.execute(
            text(
                "UPDATE oauth_credentials SET expires_at=clock_timestamp()+interval '0.05 seconds' WHERE id=:id"
            ),
            dict(id=c.credential_id),
        )
        await asyncio.sleep(0.1)
        with pytest.raises(
            admission.service().SourceReadRefused, match="credential_unavailable"
        ):
            await admission.service().check_source_read(
                conn, c, capability=foundation.capability()
            )


async def test_invoker_is_tenant_bound_and_door_metadata_grants_are_minimal(
    archive_engine, origin, sync_conn
):
    src = await admission.ready(archive_engine, origin)
    other = seed_workspace_chain(sync_conn, "read-foreign")
    async with obs.uow(archive_engine, origin) as conn:
        own = (
            (
                await conn.execute(
                    text(admission.service().AUTHORITY_SQL),
                    dict(ws=str(origin["ws"]), sid=src["source_id"], activated=True),
                )
            )
            .mappings()
            .one()
        )
        assert own["admitted"]
        foreign = (
            (
                await conn.execute(
                    text(admission.service().AUTHORITY_SQL),
                    dict(ws=str(other["ws"]), sid=str(other["src"]), activated=True),
                )
            )
            .mappings()
            .all()
        )
        assert foreign == []
        for role in ("svc_claim", "svc_clock", "svc_maintenance"):
            facts = (
                (
                    await conn.execute(
                        text(
                            "SELECT has_column_privilege(:role,'oauth_credentials','encrypted_payload','SELECT') AS secret,has_column_privilege(:role,'oauth_credentials','encrypted_payload','UPDATE') AS token_write,has_column_privilege(:role,'instagram_archive_generations','lifecycle','UPDATE') AS lifecycle_write"
                        ),
                        dict(role=role),
                    )
                )
                .mappings()
                .one()
            )
            assert not any(facts.values()), facts
        assert not (
            await conn.execute(
                text(
                    "SELECT has_function_privilege(current_user,'fn_lock_source_read_prefix(uuid)','EXECUTE')"
                )
            )
        ).scalar_one()


async def test_claim_waits_for_workspace_before_holding_job_row(
    lane_db, archive_engine, origin
):
    assert "capability" in __import__("inspect").signature(jobs.claim_job).parameters
    async with obs.uow(archive_engine, origin) as conn:
        jid = await jobs.enqueue(
            conn,
            kind="sync_media_source",
            workspace_id=str(origin["ws"]),
            serialization_key="src:" + str(origin["src"]),
            payload=dict(v=1, source_id=str(origin["src"])),
        )
    engine = create_async_engine(async_url(as_user(lane_db, "svc_worker")))
    started = asyncio.Event()

    async def claim():
        async with engine.connect() as conn:
            started.set()
            return await jobs.claim_job(
                conn, lane="bulk", worker="ordered", lease_seconds=60, ws_lane_cap=4
            )

    task = None
    try:
        async with obs.uow(archive_engine, origin) as blocker:
            await blocker.execute(
                text("SELECT id FROM workspaces WHERE id=:ws FOR UPDATE"),
                dict(ws=str(origin["ws"])),
            )
            task = asyncio.create_task(claim())
            await started.wait()
            await asyncio.sleep(0.1)
            assert not task.done()
            async with obs.uow(archive_engine, origin) as checker:
                # This would fail if the outer claim took the job first.
                assert (
                    await checker.execute(
                        text("SELECT id FROM jobs WHERE id=:id FOR UPDATE NOWAIT"),
                        dict(id=jid),
                    )
                ).scalar_one()
        claimed = await asyncio.wait_for(task, 3)
        assert str(claimed["id"]) == jid
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await engine.dispose()


@pytest.mark.parametrize("off", [False, True])
async def test_reaper_refuses_off_capture_and_never_rearms_it(
    lane_db, archive_engine, origin, sync_conn, off
):
    with sync_conn.cursor() as cur:
        cur.execute(
            "SELECT to_regprocedure('fn_reaper_sweep(integer,interval,interval,boolean)') IS NOT NULL"
        )
        assert cur.fetchone()[0], "source-aware reaper missing"
    src = await admission.ready(archive_engine, origin)
    c = await admission.capture(archive_engine, origin, src)
    async with obs.uow(archive_engine, origin) as conn:
        jid = await jobs.enqueue(
            conn,
            kind="sync_media_source",
            workspace_id=str(origin["ws"]),
            serialization_key="src:" + src["source_id"],
            payload=dict(
                v=1, source_id=src["source_id"], source_read_capture=c.payload()
            ),
        )
        if off:
            await conn.execute(
                text("UPDATE category_post_case_mix SET ratio=0 WHERE source_id=:sid"),
                dict(sid=src["source_id"]),
            )
        await conn.execute(
            text(
                "UPDATE jobs SET state='leased',lease_token=gen_random_uuid(),locked_by='dead',locked_until=now()-interval '1 second' WHERE id=:id"
            ),
            dict(id=jid),
        )
    engine = create_async_engine(async_url(as_user(lane_db, "svc_worker")))
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "SELECT fn_reaper_sweep(20,interval '1 hour',interval '1 hour',true)"
                )
            )
        with sync_conn.cursor() as cur:
            cur.execute("SELECT state,payload FROM jobs WHERE id=%s", (jid,))
            state, payload = cur.fetchone()
            assert state == ("failed" if off else "ready")
            if off:
                assert payload["source_read_refusal"] == "source_off"
            cur.execute(
                "SELECT next_sync_at FROM media_sources WHERE id=%s",
                (src["source_id"],),
            )
            assert cur.fetchone()[0] is None
    finally:
        await engine.dispose()


async def test_exhausted_rearm_requires_capture_and_current_on(archive_engine, origin):
    assert (
        "capability"
        in __import__("inspect").signature(work_loop._rearm_source).parameters
    ), "source-aware exhausted rearm missing"
    src = await admission.ready(archive_engine, origin)
    c = await admission.capture(archive_engine, origin, src)
    async with obs.uow(archive_engine, origin) as conn:
        jid = await jobs.enqueue(
            conn,
            kind="sync_media_source",
            workspace_id=str(origin["ws"]),
            serialization_key="src:" + src["source_id"],
            payload=dict(v=1, source_id=src["source_id"]),
        )
        await conn.execute(
            text(
                "UPDATE jobs SET state='leased',lease_token=gen_random_uuid(),locked_by='live',locked_until=now()+interval '1 hour' WHERE id=:id"
            ),
            dict(id=jid),
        )
        job = dict(
            (await conn.execute(text("SELECT * FROM jobs WHERE id=:id"), dict(id=jid)))
            .mappings()
            .one()
        )
        assert (
            await work_loop._rearm_source(conn, job, capability=foundation.capability())
            == 0
        )
        job["payload"]["source_read_capture"] = c.payload()
        await conn.execute(
            text("UPDATE category_post_case_mix SET ratio=0 WHERE source_id=:sid"),
            dict(sid=src["source_id"]),
        )
        assert (
            await work_loop._rearm_source(conn, job, capability=foundation.capability())
            == 0
        )


@pytest.mark.parametrize("door", ["clock", "reaper"])
async def test_outer_discovery_scope_stays_frozen_across_lock_wait(
    lane_db, archive_engine, origin, sync_conn, door
):
    """A newly due tenant waits for the next turn; no prefix is acquired late."""
    from src.services.target.unit_of_work import unit_of_work

    second = seed_workspace_chain(sync_conn, "scope-second")
    third = seed_workspace_chain(sync_conn, "scope-third")
    ids = []
    with sync_conn.cursor() as cur:
        cur.execute("UPDATE ig_accounts SET next_slot_at=NULL")
        # seed_workspace_chain also seeds cadence intents; isolate this source-job
        # concern so the third workspace is not already discovered by expiry.
        cur.execute(
            "UPDATE post_intents SET schedule_slot_at=now()+interval '1 day' WHERE workspace_id IN (%s,%s,%s)",
            (origin["ws"], second["ws"], third["ws"]),
        )
        if door == "clock":
            cur.execute(
                "UPDATE media_sources SET next_sync_at=now() WHERE workspace_id IN (%s,%s)",
                (origin["ws"], second["ws"]),
            )
        else:
            for index, chain in enumerate((origin, second, third)):
                cur.execute(
                    "INSERT INTO jobs(kind,workspace_id,lane,serialization_key,state,run_at,max_attempts,locked_by,lease_token,locked_until,payload) VALUES('sync_media_source',%s,'bulk',%s,'leased',now(),5,'dead',gen_random_uuid(),now()+make_interval(secs=>%s),jsonb_build_object('v',1,'source_id',%s)) RETURNING id",
                    (
                        chain["ws"],
                        "src:" + str(chain["src"]),
                        -60 if index < 2 else 3600,
                        str(chain["src"]),
                    ),
                )
                ids.append(cur.fetchone()[0])
    sync_conn.commit()
    engine = create_async_engine(async_url(as_user(lane_db, "svc_worker")))
    started = asyncio.Event()
    backend = []

    async def run():
        async with engine.begin() as conn:
            backend.append(
                (await conn.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            )
            started.set()
            if door == "clock":
                return await conn.execute(
                    text(
                        "SELECT * FROM fn_clock_tick(20,interval '1 hour',jsonb_build_object('v',1))"
                    )
                )
            return await conn.execute(
                text("SELECT fn_reaper_sweep(20,interval '1 hour',interval '1 hour')")
            )

    task = None
    try:
        async with unit_of_work(
            archive_engine, str(second["ws"]), actor_kind="system"
        ).begin() as blocker:
            await blocker.execute(
                text("SELECT id FROM workspaces WHERE id=:ws FOR UPDATE"),
                dict(ws=str(second["ws"])),
            )
            blocker_pid = (
                await blocker.execute(text("SELECT pg_backend_pid()"))
            ).scalar_one()
            task = asyncio.create_task(run())
            await started.wait()
            blocked = False
            for _ in range(100):
                with sync_conn.cursor() as cur:
                    cur.execute(
                        "SELECT %s=ANY(pg_blocking_pids(%s))", (blocker_pid, backend[0])
                    )
                    blocked = cur.fetchone()[0]
                if blocked:
                    break
                if task.done():
                    await task
                await asyncio.sleep(0.02)
            assert blocked, (
                "the source turn never waited on the workspace; race was not exercised"
            )
            with sync_conn.cursor() as cur:
                if door == "clock":
                    cur.execute(
                        "UPDATE media_sources SET next_sync_at=now()-interval '1 hour' WHERE id=%s",
                        (third["src"],),
                    )
                else:
                    cur.execute(
                        "UPDATE jobs SET locked_until=now()-interval '1 second' WHERE id=%s",
                        (ids[-1],),
                    )
            sync_conn.commit()
        await asyncio.wait_for(task, 3)
        with sync_conn.cursor() as cur:
            if door == "clock":
                cur.execute(
                    "SELECT count(*) FROM jobs WHERE workspace_id=%s", (third["ws"],)
                )
                assert cur.fetchone()[0] == 0
            else:
                cur.execute("SELECT state FROM jobs WHERE id=%s", (ids[-1],))
                assert cur.fetchone()[0] == "leased"
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await engine.dispose()
