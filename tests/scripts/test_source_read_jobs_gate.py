"""Demand, clock and lease lifecycle share source-read authority."""

import inspect
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from src.services.target.commands import Command, CommandRefused
from src.services.target import command_executors, jobs, media_sync, scheduler
from tests.scripts import test_source_read_admission_gate as admission
from tests.scripts import test_instagram_sources_gate as foundation
from tests.scripts import test_instagram_observations_gate as obs
from tests.scripts.conftest import async_url, as_user

pytestmark = [pytest.mark.integration]
past_posts_engine = foundation.past_posts_engine
origin = foundation.origin


def command(origin, src):
    return Command(
        kind="sync_now",
        workspace_id=str(origin["ws"]),
        actor_user_id=str(origin["user"]),
        channel="web",
        args={"source_id": src["source_id"]},
    )


async def test_demand_off_refuses_with_source_fact_and_no_job(
    past_posts_engine, origin
):
    assert "capability" in inspect.signature(command_executors.sync_now).parameters, (
        "past-posts sync admission missing"
    )
    src = await foundation.ensure(past_posts_engine, origin, foundation.authorization())
    async with obs.uow(past_posts_engine, origin) as conn:
        from tests.scripts.test_source_suitability_gate import activate

        await activate(conn, origin["ws"])
        with pytest.raises(CommandRefused) as refused:
            await command_executors.sync_now(
                conn, command(origin, src), capability=foundation.capability()
            )
        assert refused.value.facts == {
            "source_id": src["source_id"],
            "source_read_reason": "source_off",
        }
        assert (
            await conn.execute(
                text("SELECT count(*) FROM jobs WHERE payload->>'source_id'=:sid"),
                dict(sid=src["source_id"]),
            )
        ).scalar_one() == 0


async def test_demand_on_captures_authority_and_clock_closed_by_default(
    past_posts_engine, origin
):
    assert "capability" in inspect.signature(command_executors.sync_now).parameters, (
        "past-posts sync admission missing"
    )
    src = await admission.ready(past_posts_engine, origin)
    async with obs.uow(past_posts_engine, origin) as conn:
        result = await command_executors.sync_now(
            conn, command(origin, src), capability=foundation.capability()
        )
        payload = (
            await conn.execute(
                text("SELECT payload FROM jobs WHERE id=:id"),
                dict(id=result.data["job_id"]),
            )
        ).scalar_one()
        assert payload["source_read_capture"]["generation_id"] == src["generation_id"]


async def test_clock_injected_activation_mints_capture_but_off_never_mints(
    world_db, past_posts_engine, origin, sync_conn
):
    assert "capability" in inspect.signature(scheduler.tick).parameters, (
        "clock source read admission missing"
    )
    src = await admission.ready(past_posts_engine, origin)
    with sync_conn.cursor() as cur:
        cur.execute(
            "UPDATE media_sources SET next_sync_at=now() WHERE id=%s",
            (src["source_id"],),
        )
        cur.execute(
            "UPDATE ig_accounts SET next_slot_at=NULL WHERE workspace_id=%s",
            (origin["ws"],),
        )
    sync_conn.commit()
    engine = create_async_engine(async_url(as_user(world_db, "svc_worker")))
    try:
        async with engine.begin() as conn:
            await scheduler.tick(
                conn, max_inserts=20, refresh_cadence_seconds=600, recurring={"v": 1}
            )
        with sync_conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM jobs WHERE payload->>'source_id'=%s",
                (src["source_id"],),
            )
            assert cur.fetchone()[0] == 0
        async with engine.begin() as conn:
            await scheduler.tick(
                conn,
                max_inserts=20,
                refresh_cadence_seconds=600,
                recurring={"v": 1},
                capability=foundation.capability(),
            )
        with sync_conn.cursor() as cur:
            cur.execute(
                "SELECT payload FROM jobs WHERE payload->>'source_id'=%s",
                (src["source_id"],),
            )
            assert (
                cur.fetchone()[0]["source_read_capture"]["generation_id"]
                == src["generation_id"]
            )
    finally:
        await engine.dispose()


@pytest.mark.parametrize("missing_capture", [False, True])
async def test_claim_refuses_off_or_inherited_capture_and_still_claims_drive(
    world_db, past_posts_engine, origin, sync_conn, missing_capture
):
    assert "capability" in inspect.signature(jobs.claim_job).parameters, (
        "outer claim admission missing"
    )
    src = await admission.ready(past_posts_engine, origin)
    c = await admission.capture(past_posts_engine, origin, src)
    async with obs.uow(past_posts_engine, origin) as conn:
        payload = dict(v=1, source_id=src["source_id"])
        if not missing_capture:
            payload["source_read_capture"] = c.payload()
            await conn.execute(
                text("UPDATE category_post_case_mix SET ratio=0 WHERE source_id=:sid"),
                dict(sid=src["source_id"]),
            )
        refused_id = await jobs.enqueue(
            conn,
            kind="sync_media_source",
            workspace_id=str(origin["ws"]),
            serialization_key="src:" + src["source_id"],
            payload=payload,
        )
        await conn.execute(
            text("UPDATE jobs SET run_at=now()-interval '1 second' WHERE id=:id"),
            dict(id=refused_id),
        )
        drive_id = await jobs.enqueue(
            conn,
            kind="sync_media_source",
            workspace_id=str(origin["ws"]),
            serialization_key="src:" + str(origin["src"]),
            payload=dict(v=1, source_id=str(origin["src"])),
        )
    engine = create_async_engine(async_url(as_user(world_db, "svc_worker")))
    try:
        async with engine.connect() as conn:
            claimed = await jobs.claim_job(
                conn,
                lane="bulk",
                worker="fixture",
                lease_seconds=60,
                ws_lane_cap=4,
                capability=foundation.capability(),
            )
        assert str(claimed["id"]) == drive_id
        with sync_conn.cursor() as cur:
            cur.execute("SELECT state,payload FROM jobs WHERE id=%s", (refused_id,))
            state, payload = cur.fetchone()
            assert state == "failed" and payload["source_read_refusal"] == (
                "source_capture_missing" if missing_capture else "source_off"
            )
    finally:
        await engine.dispose()


async def test_past_posts_reconnect_off_does_not_clear_checkpoint_or_removed(
    past_posts_engine, origin
):
    assert (
        "capability" in inspect.signature(media_sync.rearm_after_connect).parameters
    ), "past-posts reconnect admission missing"
    src = await foundation.ensure(past_posts_engine, origin, foundation.authorization())
    async with obs.uow(past_posts_engine, origin) as conn:
        from tests.scripts.test_source_suitability_gate import activate

        await activate(conn, origin["ws"])
        await conn.execute(
            text(
                "UPDATE media_sources SET sync_checkpoint=jsonb_build_object('sentinel',1) WHERE id=:sid"
            ),
            dict(sid=src["source_id"]),
        )
        assert (
            await media_sync.rearm_after_connect(
                conn,
                workspace_id=str(origin["ws"]),
                source_id=src["source_id"],
                capability=foundation.capability(),
            )
            == 0
        )
        assert (
            await conn.execute(
                text("SELECT sync_checkpoint FROM media_sources WHERE id=:sid"),
                dict(sid=src["source_id"]),
            )
        ).scalar_one() == {"sentinel": 1}


async def test_legacy_clock_keeps_adoption_evidence_and_same_orchestration(
    past_posts_engine, origin
):
    from scripts.sql_lexer import normalize_statements

    async with obs.uow(past_posts_engine, origin) as conn:
        legacy = (
            (
                await conn.execute(
                    text(
                        "SELECT prosrc,proargnames FROM pg_proc WHERE oid='fn_clock_tick(integer,interval,jsonb)'::regprocedure"
                    )
                )
            )
            .mappings()
            .one()
        )
        activated = (
            (
                await conn.execute(
                    text(
                        "SELECT prosrc,proargnames FROM pg_proc WHERE oid='fn_clock_tick(integer,interval,jsonb,boolean)'::regprocedure"
                    )
                )
            )
            .mappings()
            .one()
        )
        # Both `fn_clock_tick` signatures preserve deadlines and expose their expected result columns.
        assert legacy["prosrc"].count("deadline_at") >= 5
        assert activated["prosrc"].count("deadline_at") >= 5
        assert legacy["proargnames"][-1] == "o_reauth_jobs"
        assert activated["proargnames"][-1] == "past_posts_reauth_jobs"
        assert normalize_statements(
            legacy["prosrc"].replace("p_past_posts_enabled boolean := false;", "")
        ) == normalize_statements(activated["prosrc"])
        assert (
            await conn.execute(
                text(
                    "SELECT count(*) FROM pg_proc WHERE proname='fn_clock_tick' AND 'o_reauth_jobs'=ANY(proargnames)"
                )
            )
        ).scalar_one() == 1


@pytest.mark.parametrize(
    "arity,enabled,refusal,healthy_kind,cross",
    [
        (3, False, "off", "drive", False),
        (3, False, "grantless", "drive", True),
        (3, False, "on", "drive", False),
        (4, True, "off", "drive", True),
        (4, True, "grantless", "drive", False),
        (4, False, "on", "drive", True),
        (4, True, "off", "past_posts", True),
        (4, True, "grantless", "past_posts", False),
    ],
)
async def test_clock_limit_one_ignores_unreadable_past_posts(
    world_db,
    past_posts_engine,
    origin,
    sync_conn,
    arity,
    enabled,
    refusal,
    healthy_kind,
    cross,
):
    from tests.scripts.conftest import seed_workspace_chain

    first = await admission.ready(past_posts_engine, origin)
    other = seed_workspace_chain(sync_conn, "clock-readable") if cross else origin
    if healthy_kind == "past_posts":
        if cross:
            with sync_conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO oauth_credentials(workspace_id,ig_account_id,provider,encrypted_payload) VALUES(%s,%s,'ig_login','fixture')",
                    (other["ws"], other["iga"]),
                )
            sync_conn.commit()
            healthy = await admission.ready(past_posts_engine, other)
        else:
            healthy = await obs.second_origin(past_posts_engine, other)
        healthy_id = healthy["source_id"]
    else:
        healthy_id = str(other["src"])
    with sync_conn.cursor() as cur:
        cur.execute("UPDATE ig_accounts SET next_slot_at=NULL")
        cur.execute("UPDATE oauth_credentials SET next_refresh_at=NULL")
        cur.execute("UPDATE media_sources SET next_sync_at=NULL")
        cur.execute(
            "UPDATE media_sources SET next_sync_at=now()-interval '2 hours' WHERE id=%s",
            (first["source_id"],),
        )
        if refusal == "off":
            cur.execute(
                "UPDATE category_post_case_mix SET ratio=0 WHERE source_id=%s",
                (first["source_id"],),
            )
        if refusal == "grantless":
            cur.execute(
                "UPDATE oauth_credentials SET state='revoked' WHERE id=%s",
                (origin["credential"],),
            )
        cur.execute(
            "SELECT next_sync_at FROM media_sources WHERE id=%s", (first["source_id"],)
        )
        before = cur.fetchone()[0]
    sync_conn.commit()
    engine = create_async_engine(async_url(as_user(world_db, "svc_worker")))
    try:
        for expected in (1, 2):
            with sync_conn.cursor() as cur:
                cur.execute(
                    "UPDATE media_sources SET next_sync_at=now()-interval '1 hour' WHERE id=%s",
                    (healthy_id,),
                )
            sync_conn.commit()
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "SELECT * FROM fn_clock_tick(1,interval '1 hour',jsonb_build_object('v',1)"
                        + (",:enabled" if arity == 4 else "")
                        + ")"
                    ),
                    dict(enabled=enabled) if arity == 4 else {},
                )
            with sync_conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM jobs WHERE payload->>'source_id'=%s",
                    (healthy_id,),
                )
                assert cur.fetchone()[0] == expected
                cur.execute(
                    "SELECT next_sync_at FROM media_sources WHERE id=%s",
                    (first["source_id"],),
                )
                assert cur.fetchone()[0] == before
                cur.execute(
                    "SELECT count(*) FROM jobs WHERE payload->>'source_id'=%s",
                    (first["source_id"],),
                )
                assert cur.fetchone()[0] == 0
    finally:
        await engine.dispose()


@pytest.mark.parametrize("arity", [3, 4])
async def test_clock_limit_one_rechecks_after_wait_and_keeps_frozen_workspaces(
    world_db, past_posts_engine, origin, sync_conn, arity
):
    import asyncio
    from tests.scripts.conftest import seed_workspace_chain

    src = await admission.ready(past_posts_engine, origin)
    first = src["source_id"] if arity == 4 else str(origin["src"])
    later = seed_workspace_chain(sync_conn, "clock-after-wait")
    with sync_conn.cursor() as cur:
        cur.execute("UPDATE ig_accounts SET next_slot_at=NULL")
        cur.execute("UPDATE oauth_credentials SET next_refresh_at=NULL")
        cur.execute("UPDATE media_sources SET next_sync_at=NULL")
        cur.execute(
            "UPDATE media_sources SET next_sync_at=now()-interval '2 hours' WHERE id=%s",
            (first,),
        )
    sync_conn.commit()
    engine = create_async_engine(async_url(as_user(world_db, "svc_worker")))
    started = asyncio.Event()
    backend = []

    async def tick():
        async with engine.begin() as conn:
            backend.append(
                (await conn.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            )
            started.set()
            return await conn.execute(
                text(
                    "SELECT * FROM fn_clock_tick(1,interval '1 hour',jsonb_build_object('v',1)"
                    + (",true" if arity == 4 else "")
                    + ")"
                )
            )

    task = None
    try:
        async with obs.uow(past_posts_engine, origin) as blocker:
            await blocker.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                dict(key="sources:" + str(origin["ws"])),
            )
            blocker_pid = (
                await blocker.execute(text("SELECT pg_backend_pid()"))
            ).scalar_one()
            task = asyncio.create_task(tick())
            await started.wait()
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
            assert blocked
            if arity == 4:
                await blocker.execute(
                    text(
                        "UPDATE category_post_case_mix SET ratio=0 WHERE source_id=:sid"
                    ),
                    dict(sid=first),
                )
            else:
                await blocker.execute(
                    text("UPDATE media_sources SET state='error' WHERE id=:sid"),
                    dict(sid=first),
                )
            with sync_conn.cursor() as cur:
                cur.execute(
                    "UPDATE media_sources SET next_sync_at=now()-interval '1 hour' WHERE id=%s",
                    (later["src"],),
                )
            sync_conn.commit()
        await asyncio.wait_for(task, 3)
        with sync_conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM jobs WHERE payload->>'source_id'=ANY(%s)",
                ([first, str(later["src"])],),
            )
            assert cur.fetchone()[0] == 0
            cur.execute(
                "SELECT next_sync_at IS NOT NULL FROM media_sources WHERE id=%s",
                (first,),
            )
            assert cur.fetchone()[0]
        await tick()
        with sync_conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM jobs WHERE payload->>'source_id'=%s",
                (str(later["src"]),),
            )
            assert cur.fetchone()[0] == 1
    finally:
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await engine.dispose()
