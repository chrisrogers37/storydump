"""The file lane through the database: the reaper's window for a past-posts
cadence story, the preparation door's fence, the sweep that mints a story's
prepare job and the executor that runs it.

`fn_reaper_sweep` leaves a past-posts cadence story alone for the window it is
given after the story's slot, so its prepare_past_post job can fetch the file,
and expires it after: a Drive story, or a call that gives no window, expires at
its slot. `fn_selected_preparation_due` leaves out a story whose
prepare_past_post job is ready or leased. The prompt sweep mints the job under
the story's tenant, a worker claims it from the media lane, and the executor
ends it by what one try at the file decides. The doors are called as
`svc_worker` calls them, and every assertion reads the database back.
"""

import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from psycopg2.extras import RealDictCursor
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.services.target import (
    instagram_publish,
    jobs,
    past_posts_preparation_job as prepare_job,
    unit_of_work,
)
from src.services.target.past_posts_retry import Retry
from src.services.target.work_loop import WorkerConfig
from src.worker import PromptSweeper
from tests.scripts import test_instagram_observations_gate as obs
from tests.scripts import test_instagram_preparation_gate as preparation
from tests.scripts import test_instagram_sources_gate as foundation
from tests.scripts import test_past_posts_composition_gate as composition
from tests.scripts.conftest import as_user, seed_workspace_chain, txn

pytestmark = [pytest.mark.integration]

past_posts_engine = foundation.past_posts_engine
origin = foundation.origin
worker = composition.worker

#: The sweep with a one-hour window, as the worker calls it once past posts are
#: activated, and as the three-argument door calls it, which gives none.
WINDOWED = (
    "SELECT fn_reaper_sweep(50, interval '24 hours', interval '72 hours',"
    " true, interval '1 hour')"
)
WINDOWLESS = "SELECT fn_reaper_sweep(50, interval '24 hours', interval '72 hours')"
DUE = "SELECT intent_id FROM fn_selected_preparation_due(50)"

#: What a story waiting on its slot is: its state, its origin and the provider of
#: the source its file belongs to.
PAST_POSTS_STORY = ("scheduled", "cadence", "instagram_past_posts")
DRIVE_STORY = ("scheduled", "cadence", "gdrive")

#: What a CDN that refused the link twice leaves on a slide: the refusal's
#: evidence and the counters of the turn that met it.
CDN_REFUSAL = {
    "v": 1,
    "kind": "cdn_refused",
    **preparation.CDN_404,
    "counters": preparation.TURN,
}

#: A job's row as the lease lifecycle leaves it: leased with a token and a lease
#: that runs on, ended with the lease cleared.
JOB_ROW = {
    "leased": "state = 'leased', locked_by = 'gate', lease_token = gen_random_uuid(),"
    " locked_until = now() + interval '1 hour'",
    "failed": "state = 'failed', locked_by = NULL, lease_token = NULL,"
    " locked_until = NULL",
    "succeeded": "state = 'succeeded', locked_by = NULL, lease_token = NULL,"
    " locked_until = NULL",
}


def as_worker(world_db, sql):
    """*sql* run as `svc_worker` runs a door, committed; the first column of its rows."""
    with txn(as_user(world_db, "svc_worker"), expect_user="svc_worker") as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = [row[0] for row in cur.fetchall()]
        conn.commit()
    return rows


def slip(sync_conn, intent, ago):
    """Move the story's slot *ago* behind now, as the owner, and read back what the
    story is: its state, its origin and its source's provider."""
    with sync_conn.cursor() as cur:
        cur.execute(
            "UPDATE post_intents SET schedule_slot_at = now() - CAST(%s AS interval)"
            " WHERE id = %s",
            (ago, intent),
        )
        assert cur.rowcount == 1
        cur.execute(
            "SELECT i.state, i.origin, s.provider FROM post_intents i"
            " JOIN media_items m ON m.workspace_id = i.workspace_id"
            " AND m.id = i.media_item_id"
            " JOIN media_sources s ON s.workspace_id = m.workspace_id"
            " AND s.id = m.source_id WHERE i.id = %s",
            (intent,),
        )
        story = cur.fetchone()
    sync_conn.commit()
    return story


def state_of(sync_conn, intent):
    with sync_conn.cursor() as cur:
        cur.execute("SELECT state FROM post_intents WHERE id = %s", (intent,))
        (state,) = cur.fetchone()
    sync_conn.commit()
    return state


async def mint(engine, chain, key):
    """A `ready` prepare_past_post job on the media lane under *key*, minted in
    the tenant's unit of work."""
    async with obs.uow(engine, chain) as conn:
        return await jobs.enqueue(
            conn,
            kind="prepare_past_post",
            workspace_id=str(chain["ws"]),
            serialization_key=key,
            payload={"v": 1},
            lane="media",
        )


def move(sync_conn, job, state):
    """The job's row in *state*, written by the owner as the lifecycle writes it."""
    with sync_conn.cursor() as cur:
        cur.execute(f"UPDATE jobs SET {JOB_ROW[state]} WHERE id = %s", (str(job),))
        assert cur.rowcount == 1
    sync_conn.commit()


def is_due(world_db, intent):
    """Whether the preparation door lists the story, called as the worker calls it."""
    return intent in {str(row) for row in as_worker(world_db, DUE)}


def prep_jobs(sync_conn, intent):
    """The jobs that hold the story's key, as the owner reads them."""
    with sync_conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT kind, lane, state, workspace_id, max_attempts, payload,"
            " deadline_at - run_at AS span FROM jobs WHERE serialization_key = %s",
            ("prep:" + intent,),
        )
        rows = cur.fetchall()
    sync_conn.commit()
    return rows


def job_row(sync_conn, job_id):
    """The job's state and attempts, whether a lease is on it, how many seconds from
    now (on the database's clock) it runs next, and its deadline."""
    with sync_conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT state, attempts, lease_token IS NOT NULL AS leased,"
            " extract(epoch FROM run_at - clock_timestamp()) AS wait, deadline_at"
            " FROM jobs WHERE id = %s",
            (str(job_id),),
        )
        row = cur.fetchone()
    sync_conn.commit()
    return row


def parked(sync_conn, intent):
    """The story's state, and the reason and the refusal its last_error keeps."""
    with sync_conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT state, last_error->'error'->>'reason' AS reason,"
            " last_error->'error'->'refusal' AS refusal"
            " FROM post_intents WHERE id = %s",
            (intent,),
        )
        row = cur.fetchone()
    sync_conn.commit()
    return row


async def minted(engine, chain, intent):
    """The story's prepare job, minted as the prompt sweep mints it: in a session
    under the story's tenant."""
    sessions = unit_of_work.make_session_for(engine)
    async with sessions({"workspace_id": str(chain["ws"])}) as session:
        return await prepare_job.enqueue(
            session,
            workspace_id=chain["ws"],
            intent_id=intent,
            config=WorkerConfig(),
        )


async def claim(engine):
    """The media lane's next job, claimed as a worker claims it; None when the
    lane has nothing the claim door will hand over."""
    async with engine.connect() as conn:
        return await jobs.claim_job(
            conn,
            lane=prepare_job.LANE,
            worker="gate",
            lease_seconds=300,
            ws_lane_cap=WorkerConfig().ws_lane_cap_for(prepare_job.LANE),
        )


async def taken(engine, chain, intent, *, attempts=1):
    """The story's prepare job, minted and claimed: leased on the media lane with
    one try consumed, or, with *attempts*, as the claim after that many tries."""
    job_id = await minted(engine, chain, intent)
    job = await claim(engine)
    assert job is not None and str(job["id"]) == job_id
    assert (job["lane"], job["state"], job["attempts"]) == ("media", "leased", 1)
    if attempts != 1:
        async with obs.uow(engine, chain) as conn:
            await conn.execute(
                text("UPDATE jobs SET attempts = :attempts WHERE id = :id"),
                dict(attempts=attempts, id=job_id),
            )
        job["attempts"] = attempts
    return job


def deps_of(engine, prepared):
    """What the executor is handed: the engine and the config, and the coordinator
    over *prepared* as its preparation and as its window park."""
    coordinator = instagram_publish.PastPostsPipeline(
        capability=foundation.capability(),
        fetch_selected=prepared.fetch_selected,
        prepare_selected=prepared.selected_prepare,
    )
    return SimpleNamespace(
        engine=engine,
        config=WorkerConfig(),
        selected_prepare=coordinator.selected_prepare,
        past_posts=coordinator,
    )


async def test_a_past_posts_cadence_story_waits_out_its_window(
    world_db, sync_conn, past_posts_engine, origin
):
    """A sweep that gives a one-hour window leaves a past-posts cadence story
    30 seconds past its slot `scheduled`, while it expires the Drive story beside
    it, so the cadence leg ran."""
    _, _, intent = await preparation.selected(past_posts_engine, origin)
    assert slip(sync_conn, intent, "30 seconds") == PAST_POSTS_STORY

    as_worker(world_db, WINDOWED)

    assert state_of(sync_conn, intent) == "scheduled"
    assert state_of(sync_conn, origin["intent"]) == "expired"


async def test_one_past_its_window_is_expired(
    world_db, sync_conn, past_posts_engine, origin
):
    """A past-posts cadence story 61 minutes past its slot is past a one-hour
    window, and the sweep expires it."""
    _, _, intent = await preparation.selected(past_posts_engine, origin)
    assert slip(sync_conn, intent, "61 minutes") == PAST_POSTS_STORY

    as_worker(world_db, WINDOWED)

    assert state_of(sync_conn, intent) == "expired"


async def test_without_a_window_it_expires_at_its_slot(
    world_db, sync_conn, past_posts_engine, origin
):
    """The three-argument door gives no window, so it expires a past-posts cadence
    story 30 seconds past its slot, as it expires a Drive story."""
    _, _, intent = await preparation.selected(past_posts_engine, origin)
    assert slip(sync_conn, intent, "30 seconds") == PAST_POSTS_STORY

    as_worker(world_db, WINDOWLESS)

    assert state_of(sync_conn, intent) == "expired"


async def test_a_drive_cadence_story_is_expired_at_its_slot(world_db, sync_conn):
    """The window is a past-posts story's alone: a Drive cadence story 30 seconds
    past its slot is expired by a sweep that gives a one-hour window."""
    chain = seed_workspace_chain(sync_conn, "drive-late")
    assert slip(sync_conn, chain["intent"], "30 seconds") == DRIVE_STORY

    as_worker(world_db, WINDOWED)

    assert state_of(sync_conn, chain["intent"]) == "expired"


async def test_the_door_leaves_out_a_story_whose_prep_job_is_live(
    world_db, sync_conn, past_posts_engine, origin
):
    """A story whose prepare_past_post job is ready or leased is not listed as due
    for preparation, and is listed again once the job has failed or succeeded. A
    live job on another story's key leaves it listed."""
    _, _, intent = await preparation.selected(past_posts_engine, origin)

    assert is_due(world_db, intent), "the story is due before any job holds it"
    await mint(past_posts_engine, origin, "prep:" + str(uuid.uuid4()))
    assert is_due(world_db, intent), "a live job on another story's key holds only it"
    for ended in ("failed", "succeeded"):
        job = await mint(past_posts_engine, origin, "prep:" + intent)
        assert not is_due(world_db, intent), "ready"
        move(sync_conn, job, "leased")
        assert not is_due(world_db, intent), "leased"
        move(sync_conn, job, ended)
        assert is_due(world_db, intent), ended


async def test_the_sweep_mints_one_prepare_job_per_due_story(
    world_db, worker, origin, sync_conn
):
    """The prompt sweep mints a ready prepare job on the media lane for a due
    past-posts story, under the story's tenant, and a second sweep mints none.
    The door then leaves the story out, so the job's key is the door's."""
    _, _, intent = await preparation.selected(worker, origin)
    config, ws = WorkerConfig(), str(origin["ws"])
    sessions = unit_of_work.make_session_for(worker)
    # The system session is no tenant's, and the policy refuses it a tenant's job:
    # only the story's own session can have minted what the sweep leaves.
    with pytest.raises(DBAPIError, match="row-level security"):
        async with sessions({}) as session:
            await prepare_job.enqueue(
                session, workspace_id=ws, intent_id=intent, config=config
            )
    app = SimpleNamespace(
        engine=worker, config=config, deps=SimpleNamespace(selected_prepare=object())
    )
    assert is_due(world_db, intent)

    await PromptSweeper(app).prepare_selected(sessions)

    (job,) = prep_jobs(sync_conn, intent)
    assert (job["kind"], job["lane"], job["state"]) == (
        "prepare_past_post",
        "media",
        "ready",
    )
    assert str(job["workspace_id"]) == ws
    assert job["max_attempts"] == config.past_posts_retry_attempts
    assert job["payload"] == {"v": 1, "intent_id": intent}
    assert job["span"] == timedelta(seconds=config.approval_ttl_seconds)
    assert not is_due(world_db, intent)

    await PromptSweeper(app).prepare_selected(sessions)

    assert len(prep_jobs(sync_conn, intent)) == 1
    async with sessions({"workspace_id": ws}) as session:
        minted_again = await prepare_job.enqueue(
            session, workspace_id=ws, intent_id=intent, config=config
        )
    assert minted_again is None, "a ready job holds the story's key"


async def test_the_executor_prepares_a_due_story_and_its_job_succeeds(
    worker, origin, sync_conn
):
    """One try that fetches the story's file leaves its slide supported and ends
    the job succeeded, claimed from the media lane; the story is the prompt
    sweep's to serve next."""
    _, _, intent = await preparation.selected(worker, origin)
    job = await taken(worker, origin, intent)
    prepared, calls = preparation.service(worker, origin)

    done = await prepare_job.execute(deps_of(worker, prepared), job)

    assert done is jobs.SELF_FINALIZED
    assert job_row(sync_conn, job["id"])["state"] == "succeeded"
    proof = await preparation.proof(worker, origin, intent)
    assert proof["validation_state"] == "supported"
    assert state_of(sync_conn, intent) == "scheduled"


async def test_an_unproven_refusal_puts_the_job_back_after_its_wait(
    worker, origin, sync_conn
):
    """A refusal that proves nothing puts the job back with its try spent, to run
    after the retry's first wait, and keeps the refusal on the slide as a fetch
    still pending; the story goes on waiting."""
    _, _, intent = await preparation.selected(worker, origin)
    job = await taken(worker, origin, intent)
    before = job_row(sync_conn, job["id"])
    prepared = preparation.refusing(
        preparation.refused("cdn_refused", **preparation.CDN_404),
        retry=Retry.from_config(WorkerConfig()),
    )

    await prepare_job.execute(deps_of(worker, prepared), job)

    after = job_row(sync_conn, job["id"])
    assert (after["state"], after["attempts"], after["leased"]) == ("ready", 1, False)
    assert 25 <= after["wait"] <= 35
    assert after["deadline_at"] == before["deadline_at"]
    capture = await preparation.capture_of(worker, origin, intent)
    assert (capture["reason"], capture["attempt"]) == ("download_pending", 1)
    assert "next_attempt_at" not in capture
    assert capture["refusal"] == CDN_REFUSAL
    assert state_of(sync_conn, intent) == "scheduled"


async def test_the_last_try_parks_the_story_for_review(worker, origin, sync_conn):
    """On the job's last try the same refusal parks the story for review with the
    refusal beside the reason, and the job ends for review."""
    config = WorkerConfig()
    _, _, intent = await preparation.selected(worker, origin)
    job = await taken(worker, origin, intent, attempts=config.past_posts_retry_attempts)
    assert job["attempts"] == job["max_attempts"]
    prepared = preparation.refusing(
        preparation.refused("cdn_refused", **preparation.CDN_404),
        retry=Retry.from_config(config),
    )

    await prepare_job.execute(deps_of(worker, prepared), job)

    story = parked(sync_conn, intent)
    assert story["state"] == "review_required"
    assert story["reason"] == "past_posts_content_unavailable"
    assert story["refusal"] == CDN_REFUSAL
    assert job_row(sync_conn, job["id"])["state"] == "review_required"


async def test_past_its_window_the_story_parks_without_a_read(
    worker, origin, sync_conn
):
    """A cadence story 59 minutes past its slot has too little of its hour left for
    an image's download, so it parks for review saying so and nothing is asked of
    Instagram; the job ends for review."""
    config = WorkerConfig()
    _, _, intent = await preparation.selected(worker, origin)
    assert slip(sync_conn, intent, "59 minutes") == PAST_POSTS_STORY
    job = await taken(worker, origin, intent)
    prepared, calls = preparation.service(worker, origin)

    await prepare_job.execute(deps_of(worker, prepared), job)

    story = parked(sync_conn, intent)
    assert story["state"] == "review_required"
    assert story["refusal"] == {
        "v": 1,
        "kind": "window_closed",
        "late_seconds": config.planned_late_seconds,
        "tries": 0,
        "last_refusal": None,
    }
    assert calls == []
    assert job_row(sync_conn, job["id"])["state"] == "review_required"


async def test_a_story_the_reaper_expired_is_told_once_and_its_job_ends(
    world_db, worker, origin, sync_conn
):
    """A cadence story the reaper expired at the end of its window while its job
    waited for the lane is told to the workspace's chat once, as a planned story's
    miss is, and the job ends cancelled; the door lists nothing to mint after it."""
    _, _, intent = await preparation.selected(worker, origin)
    assert slip(sync_conn, intent, "61 minutes") == PAST_POSTS_STORY
    async with obs.uow(worker, origin) as conn:
        binding = (
            await conn.execute(
                text(
                    "INSERT INTO channel_bindings (workspace_id, channel, external_ref)"
                    " VALUES (:ws, 'telegram_group', :ref) RETURNING id"
                ),
                dict(ws=str(origin["ws"]), ref="synthetic-" + uuid.uuid4().hex),
            )
        ).scalar_one()
    job_id = await minted(worker, origin, intent)
    as_worker(world_db, WINDOWED)
    assert state_of(sync_conn, intent) == "expired"
    job = await claim(worker)
    assert job is not None and str(job["id"]) == job_id
    prepared, calls = preparation.service(worker, origin)

    await prepare_job.execute(deps_of(worker, prepared), job)

    assert job_row(sync_conn, job_id)["state"] == "cancelled"
    assert calls == []
    async with obs.uow(worker, origin) as conn:
        told = (
            (
                await conn.execute(
                    text(
                        "SELECT payload->>'text' FROM channel_outbox"
                        " WHERE intent_id = :id AND binding_id = :binding"
                        " AND kind = 'notification'"
                    ),
                    dict(id=intent, binding=str(binding)),
                )
            )
            .scalars()
            .all()
        )
    assert len(told) == 1 and told[0].startswith("🗓 Not served")
    assert not is_due(world_db, intent)


async def test_a_workspace_holds_one_file_task_at_a_time(worker, origin, sync_conn):
    """The media lane's claim leaves a second job of the same workspace waiting
    while the first holds its lease, and hands it over once the first has ended."""
    keys = ["prep:" + str(uuid.uuid4()) for _ in range(2)]
    ids = {await mint(worker, origin, key) for key in keys}
    held = await claim(worker)
    assert held is not None and str(held["id"]) in ids
    assert await claim(worker) is None, "the workspace's one file task is running"

    move(sync_conn, held["id"], "succeeded")

    after = await claim(worker)
    assert after is not None and {str(held["id"]), str(after["id"])} == ids
