"""The file lane's prepare job: what one try decides, and how the job ends.

The database's side (the door, the reaper's window, a claim on the media lane)
is `tests/scripts/test_prepare_past_post_gate.py`; this file drives the
executor over fakes at its seams.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.services.target import (
    jobs,
    media_adapters,
    past_posts_preparation_job as prep,
    source_policy,
)
from src.services.target.work_loop import WorkerConfig

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
STORY = str(uuid.UUID(int=1))
CDN = {"v": 1, "kind": "cdn_refused", "statuses": [404, 404]}
CONFIG = WorkerConfig()


def story(**over):
    """The executor's read of a cadence story five minutes past its slot."""
    row = dict(
        state="scheduled",
        origin="cadence",
        cancel_requested=False,
        schedule_slot_at=NOW - timedelta(minutes=5),
        media_kind="image",
        now=NOW,
    )
    row.update(over)
    return row


def job(attempts=1, payload=None):
    return {
        "id": "job-1",
        "kind": "prepare_past_post",
        "workspace_id": "ws-1",
        "lease_token": "lease-1",
        "attempts": attempts,
        "max_attempts": CONFIG.past_posts_retry_attempts,
        "payload": {"v": 1, "intent_id": STORY} if payload is None else payload,
    }


class Seams:
    """What the executor did: its reads, finalizes, reschedules and parks."""

    def __init__(self, monkeypatch, row):
        self.row = row
        self.finalized, self.rescheduled = [], []
        self.prepared, self.parked_late = [], []

        def sessions(engine):
            @asynccontextmanager
            async def session_for(job):
                yield SimpleNamespace(job=job)

            return session_for

        async def read(session, sql, **params):
            assert params["id"] == STORY and params["ws"] == "ws-1"
            return self.row

        async def finalize_job(session, job_id, lease_token, state):
            self.finalized.append((job_id, lease_token, state))

        async def reschedule_job(
            session, job_id, lease_token, *, run_at, restore_attempt
        ):
            self.rescheduled.append((run_at - NOW, restore_attempt))

        monkeypatch.setattr(prep.unit_of_work, "make_session_for", sessions)
        monkeypatch.setattr(prep.readers, "row", read)
        monkeypatch.setattr(prep.jobs, "finalize_job", finalize_job)
        monkeypatch.setattr(prep.jobs, "reschedule_job", reschedule_job)
        monkeypatch.setattr(prep, "utcnow", lambda: NOW)

    def deps(self, *, prepare=None, parks_late=True):
        async def selected_prepare(**kwargs):
            self.prepared.append(kwargs)
            return await prepare() if prepare else False

        async def park_late(**kwargs):
            self.parked_late.append(kwargs)
            return parks_late

        return SimpleNamespace(
            engine=object(),
            config=CONFIG,
            past_posts=SimpleNamespace(
                selected_prepare=selected_prepare, park_late=park_late
            ),
        )


def raising(error):
    async def prepare():
        raise error

    return prepare


def returning(value):
    async def prepare():
        return value

    return prepare


@pytest.mark.parametrize(
    "parked,state", [(False, "succeeded"), (True, "review_required")]
)
async def test_a_try_ends_the_job_with_what_the_preparation_did(
    monkeypatch, parked, state
):
    """A prepared file ends the job succeeded; a story the preparation parked
    ends it for review. The try's number is the job's attempts."""
    seams = Seams(monkeypatch, story())
    done = await prep.execute(seams.deps(prepare=returning(parked)), job(attempts=3))
    assert done is jobs.SELF_FINALIZED
    assert [(p["intent_id"], p["attempt"]) for p in seams.prepared] == [(STORY, 3)]
    assert seams.finalized == [("job-1", "lease-1", state)]
    assert seams.rescheduled == []


async def test_an_unproven_refusal_spends_the_try_and_waits_the_retrys_delay(
    monkeypatch,
):
    """A refusal that proves nothing puts the job back after the doubling wait
    for this try, the try spent."""
    seams = Seams(monkeypatch, story())
    refused = source_policy.MediaAdmissionPending(
        "past_posts_revalidation_required", evidence=CDN
    )
    await prep.execute(seams.deps(prepare=raising(refused)), job(attempts=3))
    delay = CONFIG.past_posts_retry_base_seconds * 2**2
    assert seams.rescheduled == [(timedelta(seconds=delay), False)]
    assert seams.finalized == []


async def test_a_wait_that_read_nothing_gives_its_try_back(monkeypatch):
    seams = Seams(monkeypatch, story())
    waits = source_policy.MediaAdmissionPending("past_posts_revalidation_required")
    await prep.execute(seams.deps(prepare=raising(waits)), job(attempts=3))
    assert seams.rescheduled == [
        (timedelta(seconds=CONFIG.past_posts_retry_base_seconds), True)
    ]


@pytest.mark.parametrize(
    "error,spent",
    [
        (media_adapters.MediaRetryableError("cdn 503"), True),
        (media_adapters.MediaLostResponse(), True),
        (media_adapters.MediaCredentialDead("token"), True),
        (media_adapters.MediaSourceFenceDrop(), False),
        (media_adapters.MediaSourceGone("source"), False),
    ],
    ids=["retryable", "lost-answer", "dead-credential", "fence-drop", "source-gone"],
)
async def test_a_provider_answer_spends_the_try_and_a_closed_source_does_not(
    monkeypatch, error, spent
):
    """Instagram asked and refused: the try is spent, after the retry's wait.
    A source that no longer admits the read asked nothing: the try comes back.
    A fence drop is a retryable error, and still asked nothing."""
    seams = Seams(monkeypatch, story())
    await prep.execute(seams.deps(prepare=raising(error)), job(attempts=2))
    wait = (
        CONFIG.past_posts_retry_base_seconds * 2
        if spent
        else CONFIG.past_posts_retry_base_seconds
    )
    assert seams.rescheduled == [(timedelta(seconds=wait), not spent)]


async def test_past_its_window_a_cadence_story_parks_before_any_read(monkeypatch):
    """A download that could not finish inside the story's late window is not
    started: the story parks for review with how long it had and the tries
    before this one, the claim that brought the job here being no try."""
    seams = Seams(monkeypatch, story(schedule_slot_at=NOW - timedelta(minutes=59)))
    deps = seams.deps()
    await prep.execute(deps, job(attempts=3))
    assert seams.prepared == []
    assert seams.parked_late == [
        {
            "engine": deps.engine,
            "workspace_id": "ws-1",
            "intent_id": STORY,
            "tries": 2,
            "late_seconds": CONFIG.planned_late_seconds,
        }
    ]
    assert seams.finalized == [("job-1", "lease-1", "review_required")]


@pytest.mark.parametrize(
    "kind,parks", [("image", False), ("video", True)], ids=["image", "video"]
)
async def test_the_window_closes_by_the_kinds_download_budget(monkeypatch, kind, parks):
    """Three minutes before the window ends, an image's download can still
    finish inside it; a video's longer budget cannot."""
    late = CONFIG.planned_late_seconds
    slot = NOW - timedelta(seconds=late) + timedelta(minutes=3)
    assert (
        CONFIG.past_posts_download_seconds["image"]
        < 180
        < CONFIG.past_posts_download_seconds["video"]
    )
    seams = Seams(monkeypatch, story(schedule_slot_at=slot, media_kind=kind))
    await prep.execute(seams.deps(), job())
    assert (len(seams.parked_late), len(seams.prepared)) == (
        (1, 0) if parks else (0, 1)
    )


async def test_a_window_park_that_finds_the_story_moved_on_looks_again(
    monkeypatch,
):
    seams = Seams(monkeypatch, story(schedule_slot_at=NOW - timedelta(hours=2)))
    await prep.execute(seams.deps(parks_late=False), job(attempts=4))
    assert seams.finalized == [] and seams.prepared == []
    assert seams.rescheduled == [
        (timedelta(seconds=CONFIG.past_posts_retry_base_seconds), True)
    ]


@pytest.mark.parametrize(
    "state,origin",
    [("awaiting_approval", "cadence"), ("scheduled", "planned")],
    ids=["served", "planned"],
)
async def test_no_window_holds_a_served_or_a_planned_story(monkeypatch, state, origin):
    """A served story's bound is its approval TTL, a planned story's the miss
    leg's: both are prepared however late."""
    seams = Seams(
        monkeypatch,
        story(state=state, origin=origin, schedule_slot_at=NOW - timedelta(hours=2)),
    )
    await prep.execute(seams.deps(), job())
    assert seams.parked_late == [] and len(seams.prepared) == 1


@pytest.mark.parametrize(
    "over",
    [
        dict(state="expired"),
        dict(state="cancelled"),
        dict(state="approved"),
        dict(state="review_required"),
        dict(cancel_requested=True),
    ],
    ids=["expired", "cancelled", "approved", "parked", "cancel-asked"],
)
async def test_a_story_that_moved_on_ends_the_job(monkeypatch, over):
    """A story no longer waiting for its file, or one whose cancel was asked,
    ends the job cancelled, with nothing fetched and nothing parked."""
    seams = Seams(monkeypatch, story(**over))
    await prep.execute(seams.deps(), job())
    assert seams.prepared == [] and seams.parked_late == []
    assert seams.finalized == [("job-1", "lease-1", "cancelled")]


async def test_a_story_gone_ends_the_job(monkeypatch):
    seams = Seams(monkeypatch, None)
    await prep.execute(seams.deps(), job())
    assert seams.finalized == [("job-1", "lease-1", "cancelled")]


async def test_a_payload_that_names_no_story_fails_the_job(monkeypatch):
    seams = Seams(monkeypatch, story())
    await prep.execute(seams.deps(), job(payload={"v": 1, "intent_id": "x"}))
    assert seams.finalized == [("job-1", "lease-1", "failed")]
    assert seams.prepared == []


class _ParkRead:
    """A session that answers the exhausted flip's read with *row*."""

    def __init__(self, row):
        self.row = row

    async def execute(self, statement, params):
        assert params == {"intent": STORY}
        return SimpleNamespace(mappings=lambda: SimpleNamespace(first=lambda: self.row))


@pytest.mark.parametrize("state", ["scheduled", "prompt_pending", "awaiting_approval"])
async def test_a_spent_prepare_job_parks_its_waiting_story_saying_what_it_tried(
    monkeypatch, state
):
    """A prepare job whose faults spend its budget parks its story through the
    publish pipeline's flip, from any state the story waits in, saying it was
    fetching the story's file."""
    from src.services.target import publish_pipeline, vocabulary

    flipped = []

    async def flip_to_review(session, *, intent_id, from_state):
        flipped.append((intent_id, from_state))
        return True

    monkeypatch.setattr(publish_pipeline, "flip_to_review", flip_to_review)
    parked = await publish_pipeline.flip_exhausted(
        _ParkRead({"state": state, "workspace_id": "ws-1", "tz": "UTC"}),
        job(attempts=8),
        from_states=vocabulary.WAITING_STATES,
        trying_to="fetch this story's file from Instagram",
    )
    assert flipped == [(STORY, state)]
    assert parked["notice"].startswith(
        "We hit a fault on our side 8 times trying to fetch this story's file"
    )


async def test_a_spent_prepare_job_leaves_a_story_that_moved_on(monkeypatch):
    from src.services.target import publish_pipeline, vocabulary

    async def flip_to_review(session, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("a story that moved on is not flipped")

    monkeypatch.setattr(publish_pipeline, "flip_to_review", flip_to_review)
    parked = await publish_pipeline.flip_exhausted(
        _ParkRead({"state": "approved", "workspace_id": "ws-1", "tz": "UTC"}),
        job(),
        from_states=vocabulary.WAITING_STATES,
        trying_to="fetch this story's file from Instagram",
    )
    assert parked is None
