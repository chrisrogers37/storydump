"""The file lane: each past-posts story's file is fetched by a job of its own.

The prompt sweep mints one `prepare_past_post` job per story whose file is due
(`fn_selected_preparation_due`, which leaves out a story whose job is ready or
running), on the `media` lane and keyed `prep:<intent id>`, so a story never
runs twice at once. The sweep fetches nothing itself, and a slow download
holds one media loop and nothing else.

Each claim is one try, which `fn_claim_job` counts in the job's attempts:

- A cadence story still waiting for its slot is parked for review, before any
  read, once a download could no longer finish inside its late window
  (`WorkerConfig.planned_late_seconds`, the window a planned story has). The
  reaper never expires such a story: it parks one whose window closed with no
  job running (`fn_reaper_late_past_posts`), the same way. A planned story's
  window is the miss leg's (`fn_planned_misses`), which tells its chats.
- Otherwise the coordinator prepares the file
  (`PastPostsPipeline.selected_prepare`). The preparation decides each refusal
  on this try's number (`past_posts_retry`): a refusal that proves nothing puts
  the job back after the retry's wait, with the try spent; a proven refusal or
  the last try parks the story with its evidence. A wait that read nothing
  puts the job back with the try restored.

A job whose untyped faults spend its budget parks its story through
`publish_pipeline.flip_exhausted`, as a spent publish job's story is.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Optional

from src.services.target import (
    jobs,
    media_adapters,
    past_posts_retry,
    readers,
    source_policy,
    unit_of_work,
    vocabulary,
)
from src.utils.datetime_utils import utcnow

logger = logging.getLogger(__name__)

KIND = "prepare_past_post"
LANE = "media"

_STORY = """
SELECT i.state, i.origin, i.cancel_requested, i.schedule_slot_at, m.media_kind,
       clock_timestamp() AS now
  FROM post_intents i
  JOIN media_items m ON m.workspace_id = i.workspace_id AND m.id = i.media_item_id
 WHERE i.workspace_id = :ws AND i.id = :id
"""


def serialization_key(intent_id) -> str:
    return f"prep:{intent_id}"


async def enqueue(session, *, workspace_id, intent_id, config) -> Optional[str]:
    """Mint the story's prepare job, in the caller's tenant transaction, unless
    one is ready or running. Its budget is the retry's attempts
    (`WorkerConfig.past_posts_retry_attempts`); its deadline is the approval
    TTL, the longest a story waits for its file. Returns the job id, or None."""
    return await jobs.enqueue(
        session,
        kind=KIND,
        workspace_id=str(workspace_id),
        serialization_key=serialization_key(intent_id),
        payload={"v": 1, "intent_id": str(intent_id)},
        lane=LANE,
        max_attempts=config.past_posts_retry_attempts,
        deadline_seconds=config.approval_ttl_seconds,
        unless_pending=True,
    )


def _intent_of(job) -> Optional[str]:
    payload = job.get("payload") or {}
    try:
        return str(uuid.UUID(str(payload.get("intent_id"))))
    except (TypeError, ValueError):
        return None


def _past_window(story, config) -> bool:
    """Whether a download started now could no longer finish inside the
    story's late window, on the database's clock."""
    budget = config.past_posts_download_seconds.get(story["media_kind"], 0)
    return story["now"] + timedelta(seconds=budget) > story[
        "schedule_slot_at"
    ] + timedelta(seconds=config.planned_late_seconds)


async def _end(deps, job, state: str) -> str:
    sessions = unit_of_work.make_session_for(deps.engine)
    async with sessions(job) as session:
        await jobs.finalize_job(session, job["id"], job["lease_token"], state)
    return state


async def _put_back(deps, job, *, spent: bool) -> str:
    """Run the job again: after the retry's wait for this try when the try was
    *spent*, else after the retry's base wait, with the try given back."""
    retry = past_posts_retry.Retry.from_config(deps.config)
    seconds = retry.delay(int(job["attempts"])) if spent else retry.base_seconds
    sessions = unit_of_work.make_session_for(deps.engine)
    async with sessions(job) as session:
        await jobs.reschedule_job(
            session,
            job["id"],
            job["lease_token"],
            run_at=utcnow() + timedelta(seconds=seconds),
            restore_attempt=not spent,
        )
    return "tried again later" if spent else "waited"


async def execute(deps, job) -> str:
    """One try at a story's file; the job finalizes or reschedules itself.
    Returns `jobs.SELF_FINALIZED`."""
    outcome = await _turn(deps, job)
    logger.info("prepare_past_post %s -> %s", job["id"], outcome)
    return jobs.SELF_FINALIZED


async def _turn(deps, job) -> str:
    ws, iid = str(job["workspace_id"]), _intent_of(job)
    if iid is None:
        logger.warning(
            "prepare_past_post %s: its payload names no story; nothing prepared",
            job["id"],
        )
        return await _end(deps, job, "failed")
    sessions = unit_of_work.make_session_for(deps.engine)
    async with sessions(job) as session:
        story = await readers.row(session, _STORY, ws=ws, id=iid)
    if story is None:
        return await _end(deps, job, "cancelled")
    if story["state"] not in vocabulary.WAITING_STATES or story["cancel_requested"]:
        # Moved on: served, parked, approved, cancelled or ended.
        return await _end(deps, job, "cancelled")
    if (
        story["origin"] == "cadence"
        and story["state"] in vocabulary.UNSERVED_STATES
        and _past_window(story, deps.config)
    ):
        if await deps.past_posts.park_late(
            engine=deps.engine,
            workspace_id=ws,
            intent_id=iid,
            tries=max(int(job["attempts"]) - 1, 0),
            late_seconds=deps.config.planned_late_seconds,
        ):
            return await _end(deps, job, "review_required")
        # It moved on between the read and the lock: look again.
        return await _put_back(deps, job, spent=False)
    try:
        parked = await deps.past_posts.selected_prepare(
            engine=deps.engine,
            workspace_id=ws,
            intent_id=iid,
            attempt=int(job["attempts"]),
        )
    except source_policy.MediaAdmissionPending as exc:
        # With evidence, a try was refused and the refusal proved nothing yet.
        return await _put_back(deps, job, spent=bool(exc.evidence))
    except (media_adapters.MediaSourceFenceDrop, media_adapters.MediaSourceGone):
        # The source no longer admits the read: nothing was asked, and the
        # story's window still bounds it. (A fence drop is a retryable error,
        # so it is caught first.)
        return await _put_back(deps, job, spent=False)
    except (
        media_adapters.MediaRetryableError,
        media_adapters.MediaLostResponse,
        media_adapters.MediaCredentialDead,
    ):
        # Instagram was asked and did not answer with the file: the try is
        # spent. A dead connection is told by its own reauth prompt; its story
        # keeps trying, further apart, until its window or TTL.
        return await _put_back(deps, job, spent=True)
    return await _end(deps, job, "review_required" if parked else "succeeded")
