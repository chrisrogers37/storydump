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
  reaper keeps such a story that long after its slot (`fn_reaper_sweep`). A
  planned story's window is the miss leg's (`fn_planned_misses`), which tells
  its chats.
- Otherwise the coordinator prepares the file
  (`PastPostsPipeline.selected_prepare`). The preparation decides each refusal
  on this try's number (`past_posts_retry`): a refusal that proves nothing puts
  the job back after the retry's wait, with the try spent; a proven refusal or
  the last try parks the story with its evidence. A wait that read nothing
  puts the job back with the try restored.
- A cadence story the reaper expired while its job waited for the lane is told
  once, as a planned story's miss is (`prompts.say_not_served`), so no expiry
  of a story waiting for its file is silent.
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
    prompts,
    publish_pipeline,
    readers,
    refusal_evidence,
    source_policy,
    unit_of_work,
)
from src.utils.datetime_utils import utcnow

logger = logging.getLogger(__name__)

KIND = "prepare_past_post"
LANE = "media"

#: The states a story waits in for its file: the door's subjects.
_WAITING = ("scheduled", "prompt_pending", "awaiting_approval")
#: The states the reaper's cadence leg expires a story from.
_BEFORE_SLOT = ("scheduled", "prompt_pending")

_STORY = """
SELECT i.id, i.workspace_id, i.state, i.origin, i.cancel_requested,
       i.schedule_slot_at, m.media_kind, m.file_name, acc.handle, w.tz,
       a.validation_capture AS capture, clock_timestamp() AS now,
       (SELECT e.from_state FROM audit_events e
         WHERE e.workspace_id = i.workspace_id AND e.entity_kind = 'post_intent'
           AND e.entity_id = i.id AND e.to_state = 'expired'
         ORDER BY e.id DESC LIMIT 1) AS expired_from
  FROM post_intents i
  JOIN workspaces w ON w.id = i.workspace_id
  JOIN media_items m ON m.workspace_id = i.workspace_id AND m.id = i.media_item_id
  LEFT JOIN ig_accounts acc ON acc.workspace_id = i.workspace_id
   AND acc.id = i.ig_account_id
  LEFT JOIN instagram_media_assets a ON a.workspace_id = m.workspace_id
   AND a.source_id = m.source_id AND a.media_item_id = m.id
 WHERE i.workspace_id = :ws AND i.id = :id
"""

_PARKABLE = """
SELECT i.state, i.workspace_id, COALESCE(acc.tz, w.tz) AS tz
  FROM post_intents i
  JOIN workspaces w ON w.id = i.workspace_id
  LEFT JOIN ig_accounts acc ON acc.id = i.ig_account_id
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


def _late_evidence(story, job, config) -> dict:
    capture = story["capture"] if isinstance(story["capture"], dict) else {}
    return refusal_evidence.evidence(
        "window_closed",
        late_seconds=config.planned_late_seconds,
        tries=max(int(job["attempts"]) - 1, 0),
        last_refusal=capture.get("refusal"),
    )


async def _end(deps, job, state: str, *, told: Optional[dict] = None) -> str:
    """Finalize the job *state*; with *told*, first tell the story's chats it
    was not served, in the same transaction."""
    sessions = unit_of_work.make_session_for(deps.engine)
    async with sessions(job) as session:
        if told is not None:
            await prompts.say_not_served(
                session,
                told,
                reason="late",
                surface=await prompts.push_bindings(session, str(job["workspace_id"])),
                by=None,
            )
        await jobs.finalize_job(session, job["id"], job["lease_token"], state)
    return state


async def _put_back(deps, job, seconds: float, *, spent: bool) -> str:
    """Run the job again after *seconds*: a try *spent*, or a wait that read
    nothing and gives its try back."""
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
    cfg = deps.config
    retry = past_posts_retry.Retry.from_config(cfg)
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
    if story["state"] not in _WAITING or story["cancel_requested"]:
        # Moved on: served, parked, approved, cancelled or ended. A cadence
        # story the reaper expired at the end of its window while this job
        # waited is told, once: no other job of it is minted after.
        expired_late = (
            story["state"] == "expired"
            and story["origin"] == "cadence"
            and story["expired_from"] in _BEFORE_SLOT
        )
        return await _end(deps, job, "cancelled", told=story if expired_late else None)
    if (
        story["origin"] == "cadence"
        and story["state"] in _BEFORE_SLOT
        and _past_window(story, cfg)
    ):
        if await deps.past_posts.park_late(
            engine=deps.engine,
            workspace_id=ws,
            intent_id=iid,
            evidence=_late_evidence(story, job, cfg),
        ):
            return await _end(deps, job, "review_required")
        # It moved on between the read and the lock: look again.
        return await _put_back(deps, job, retry.base_seconds, spent=False)
    try:
        parked = await deps.selected_prepare(
            engine=deps.engine,
            workspace_id=ws,
            intent_id=iid,
            attempt=int(job["attempts"]),
        )
    except source_policy.MediaAdmissionPending as exc:
        if exc.evidence:
            # A try was refused and the refusal proved nothing yet.
            return await _put_back(
                deps, job, retry.delay(int(job["attempts"])), spent=True
            )
        return await _put_back(deps, job, retry.base_seconds, spent=False)
    except (media_adapters.MediaSourceFenceDrop, media_adapters.MediaSourceGone):
        # The source no longer admits the read: nothing was asked, and the
        # story's window still bounds it. (A fence drop is a retryable error,
        # so it is caught first.)
        return await _put_back(deps, job, retry.base_seconds, spent=False)
    except (
        media_adapters.MediaRetryableError,
        media_adapters.MediaLostResponse,
        media_adapters.MediaCredentialDead,
    ):
        # Instagram was asked and did not answer with the file: the try is
        # spent. A dead connection is told by its own reauth prompt; its story
        # keeps trying, further apart, until its window or TTL.
        return await _put_back(deps, job, retry.delay(int(job["attempts"])), spent=True)
    return await _end(deps, job, "review_required" if parked else "succeeded")


async def flip_exhausted(session, job) -> Optional[dict]:
    """A prepare job whose budget is spent on untyped faults: its story, still
    waiting for its file, is parked for review. Returns the `tell_review`
    arguments, or None when nothing was parked."""
    iid = _intent_of(job)
    if iid is None or job.get("workspace_id") is None:
        return None
    row = await readers.row(session, _PARKABLE, ws=str(job["workspace_id"]), id=iid)
    if row is None or row["state"] not in _WAITING:
        return None
    if not await publish_pipeline.flip_to_review(
        session, intent_id=iid, from_state=row["state"]
    ):
        return None
    tries = int(job.get("attempts") or job.get("max_attempts") or 0)
    return {
        "workspace_id": str(row["workspace_id"]),
        "intent_id": iid,
        "tz": str(row["tz"] or "UTC"),
        "notice": (
            f"We hit a fault on our side {tries} times trying to fetch this"
            " story's file from Instagram: choose on its card, or open the Queue"
            " on the web."
        ),
    }
