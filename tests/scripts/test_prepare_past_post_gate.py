"""The file lane through the database: what the reaper does with a past-posts
cadence story, the preparation door's fence, the sweep that mints a story's
prepare job and the executor that runs it.

`fn_reaper_sweep` leaves a past-posts cadence story of an active, unpaused
workspace to a caller that parks it (`p_past_posts_park_late`), and that
caller's reaper parks it for review once its window has closed with no prepare
job running (`fn_reaper_late_past_posts`). Any other call, the four-argument
one an older worker makes included, expires it at its slot, as it expires a
Drive story. `fn_selected_preparation_due` leaves out a story whose
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
    scheduler,
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

#: The sweep as a worker that parks late past-posts stories calls it, and the
#: calls that expire them at their slot: the four-argument one an older worker
#: makes, and the three-argument door.
PARKS = (
    "SELECT fn_reaper_sweep(50, interval '24 hours', interval '72 hours', true, true)"
)
EXPIRES = {
    "four-arguments": (
        "SELECT fn_reaper_sweep(50, interval '24 hours', interval '72 hours', true)"
    ),
    "three-arguments": (
        "SELECT fn_reaper_sweep(50, interval '24 hours', interval '72 hours')"
    ),
}
DUE = "SELECT intent_id FROM fn_selected_preparation_due(50)"
LATE = (
    "SELECT o_intent_id, o_tries FROM fn_reaper_late_past_posts(interval '1 hour', 50)"
)

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


def late(world_db):
    """The stories the late door lists for a one-hour window, called as the
    worker calls it, each with its tries."""
    with txn(as_user(world_db, "svc_worker"), expect_user="svc_worker") as conn:
        with conn.cursor() as cur:
            cur.execute(LATE)
            rows = {str(intent): tries for intent, tries in cur.fetchall()}
        conn.commit()
    return rows


def pause(sync_conn, ws):
    """Pause the workspace, as the owner."""
    with sync_conn.cursor() as cur:
        cur.execute("UPDATE workspaces SET is_paused = true WHERE id = %s", (str(ws),))
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


async def bind(engine, chain):
    """A group chat bound to the workspace, so a notice has a chat to go to."""
    async with obs.uow(engine, chain) as conn:
        return str(
            (
                await conn.execute(
                    text(
                        "INSERT INTO channel_bindings (workspace_id, channel, external_ref)"
                        " VALUES (:ws, 'telegram_group', :ref) RETURNING id"
                    ),
                    dict(ws=str(chain["ws"]), ref="synthetic-" + uuid.uuid4().hex),
                )
            ).scalar_one()
        )


async def notices(engine, chain, intent, binding):
    """The texts of the notifications queued for the story in the bound chat."""
    async with obs.uow(engine, chain) as conn:
        return list(
            (
                await conn.execute(
                    text(
                        "SELECT payload->>'text' FROM channel_outbox"
                        " WHERE intent_id = :id AND binding_id = :binding"
                        " AND kind = 'notification' ORDER BY created_at, id"
                    ),
                    dict(id=intent, binding=binding),
                )
            )
            .scalars()
            .all()
        )


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
        past_posts=coordinator,
    )


async def test_a_caller_that_parks_leaves_a_late_past_posts_story_waiting(
    world_db, sync_conn, past_posts_engine, origin
):
    """A sweep from a worker that parks late past-posts stories leaves a
    past-posts cadence story 61 minutes past its slot `scheduled`, while it
    expires the Drive story beside it, so the cadence leg ran."""
    _, _, intent = await preparation.selected(past_posts_engine, origin)
    assert slip(sync_conn, intent, "61 minutes") == PAST_POSTS_STORY

    as_worker(world_db, PARKS)

    assert state_of(sync_conn, intent) == "scheduled"
    assert state_of(sync_conn, origin["intent"]) == "expired"


@pytest.mark.parametrize("call", sorted(EXPIRES))
async def test_any_other_call_expires_it_at_its_slot(
    world_db, sync_conn, past_posts_engine, origin, call
):
    """The four-argument call an older worker makes, and the three-argument
    door, expire a past-posts cadence story 30 seconds past its slot, as they
    expire a Drive story: the new argument's default keeps what they did."""
    _, _, intent = await preparation.selected(past_posts_engine, origin)
    assert slip(sync_conn, intent, "30 seconds") == PAST_POSTS_STORY

    as_worker(world_db, EXPIRES[call])

    assert state_of(sync_conn, intent) == "expired"


async def test_a_drive_cadence_story_is_expired_at_its_slot(world_db, sync_conn):
    """Only a past-posts story is left to the caller: a sweep from a worker
    that parks expires a Drive cadence story 30 seconds past its slot."""
    chain = seed_workspace_chain(sync_conn, "drive-late")
    assert slip(sync_conn, chain["intent"], "30 seconds") == DRIVE_STORY

    as_worker(world_db, PARKS)

    assert state_of(sync_conn, chain["intent"]) == "expired"


async def test_a_paused_workspaces_story_expires_and_is_never_listed(
    world_db, sync_conn, past_posts_engine, origin
):
    """A paused workspace's past-posts cadence story is neither served nor
    parked: the late door never lists it, and a sweep from a worker that parks
    expires it at its slot, as before."""
    _, _, intent = await preparation.selected(past_posts_engine, origin)
    assert slip(sync_conn, intent, "61 minutes") == PAST_POSTS_STORY
    assert intent in late(world_db), "listed while its workspace posts"
    pause(sync_conn, origin["ws"])
    assert intent not in late(world_db)

    as_worker(world_db, PARKS)

    assert state_of(sync_conn, intent) == "expired"


async def test_the_late_door_lists_a_story_past_its_window_with_its_tries(
    world_db, sync_conn, past_posts_engine, origin
):
    """The late door lists a waiting past-posts cadence story only once it is a
    window past its slot, with the tries its latest prepare job spent, and
    leaves it out while that job's try is running."""
    _, _, intent = await preparation.selected(past_posts_engine, origin)
    assert slip(sync_conn, intent, "59 minutes") == PAST_POSTS_STORY
    assert intent not in late(world_db), "inside its window"
    assert slip(sync_conn, intent, "61 minutes") == PAST_POSTS_STORY
    assert late(world_db)[intent] == 0, "no job has tried it"

    job = await mint(past_posts_engine, origin, "prep:" + intent)
    with sync_conn.cursor() as cur:
        cur.execute("UPDATE jobs SET attempts = 3 WHERE id = %s", (str(job),))
        assert cur.rowcount == 1
    sync_conn.commit()
    assert late(world_db)[intent] == 3, "ready, between tries"
    move(sync_conn, job, "leased")
    assert intent not in late(world_db), "a try is running"
    move(sync_conn, job, "failed")
    assert late(world_db)[intent] == 3, "ended"


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
        engine=worker, config=config, deps=SimpleNamespace(past_posts=object())
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


async def test_the_reaper_parks_a_late_story_saying_why_and_tells_it_once(
    worker, origin, sync_conn
):
    """The reaper of a worker that parks: a past-posts cadence story whose try
    was refused, still waiting 61 minutes past its slot, is parked for review.
    Its evidence says the window closed, how many tries it had and the refusal
    its slide recorded, and its chat is told once. Its job, run after, ends
    cancelled and tells nothing more."""
    config = WorkerConfig()
    _, _, intent = await preparation.selected(worker, origin)
    job = await taken(worker, origin, intent)
    prepared = preparation.refusing(
        preparation.refused("cdn_refused", **preparation.CDN_404),
        retry=Retry.from_config(config),
    )
    deps = deps_of(worker, prepared)
    await prepare_job.execute(deps, job)
    assert job_row(sync_conn, job["id"])["state"] == "ready"
    assert slip(sync_conn, intent, "61 minutes") == PAST_POSTS_STORY
    binding = await bind(worker, origin)

    await scheduler.execute_reap_expired(
        worker,
        limit=50,
        approval_ttl_seconds=config.approval_ttl_seconds,
        approved_ttl_seconds=config.approved_ttl_seconds,
        capability=foundation.capability(),
        park_late=deps.past_posts.park_late,
        past_posts_late_seconds=config.planned_late_seconds,
    )

    story = parked(sync_conn, intent)
    assert story["state"] == "review_required"
    assert story["reason"] == "past_posts_content_unavailable"
    assert story["refusal"] == {
        "v": 1,
        "kind": "window_closed",
        "late_seconds": config.planned_late_seconds,
        "tries": 1,
        "last_refusal": CDN_REFUSAL,
    }
    (told,) = await notices(worker, origin, intent, binding)
    assert "could not be fetched within 60 minutes of its slot (1 try)" in told

    with sync_conn.cursor() as cur:
        cur.execute("UPDATE jobs SET run_at = now() WHERE id = %s", (str(job["id"]),))
        assert cur.rowcount == 1
    sync_conn.commit()
    again = await claim(worker)
    assert again is not None and str(again["id"]) == str(job["id"])
    await prepare_job.execute(deps, again)

    assert job_row(sync_conn, job["id"])["state"] == "cancelled"
    assert await notices(worker, origin, intent, binding) == [told]
    assert parked(sync_conn, intent)["state"] == "review_required"


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
