"""A story's transit copy goes when the story ends (FC-3.5), at the unit.

Posted and cancelled destroyed it inline already; a FAILED story and a story
GIVEN UP from review waited for the 48 h sweep, which runs every 6 h — up to
~54 h on Cloudinary for a story nobody will post. `_fail_terminal` now
destroys after its commit, and a job meeting a terminal intent (the job a
give-up mints, since the API holds no transit credentials) destroys before
it finalizes. Every destroy is best-effort: no ref is a no-op and an error is
logged and swallowed — the sweep is the guarantee. The same paths against a
real database are `tests/scripts/test_l5_pipeline_gate.py`."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import pytest

from src.services.target import publish_pipeline
from src.services.target.drive_adapter import DriveMediaTooLarge
from src.services.target.publish_pipeline import (
    CANCELLED,
    FAILED,
    _Ctx,
    _destroy_transit_best_effort,
    _fail_terminal,
)

JOB = {"id": "job-1", "lease_token": "tok", "workspace_id": "ws", "payload": {}}


def _ctx(state="publishing", ref="ws/x/t1"):
    return _Ctx(
        dict(JOB),
        {
            "id": "i1",
            "state": state,
            "transit_asset_ref": ref,
            "media_kind": "image",
            "ig_account_id": "a1",
            "eff_tz": "UTC",
            "attempts_by_step": {"v": 1},
        },
        [],
    )


class _Transit:
    def __init__(self, raises=False):
        self.calls: list[tuple[str, str]] = []
        self._raises = raises

    async def destroy(self, ref, *, media_kind):
        self.calls.append((ref, media_kind))
        if self._raises:
            raise ConnectionError("destroy transport lost")
        return True


class _Row:
    def fetchone(self):
        return ("i1",)


class _Session:
    async def execute(self, statement, params=None):
        return _Row()


class _Uow:
    @asynccontextmanager
    async def begin(self):
        yield _Session()


@pytest.fixture
def world(monkeypatch):
    """The terminal transaction's seams, recorded in the order they ran, so a
    destroy can be shown to follow the commit rather than sit inside it."""
    log: list[str] = []

    async def finalize_job(session, job_id, token, outcome):
        log.append(f"finalize:{outcome}")

    async def nothing(*a, **kw):
        return None

    monkeypatch.setattr(publish_pipeline, "finalize_job", finalize_job)
    monkeypatch.setattr(publish_pipeline, "assert_lease", nothing)
    monkeypatch.setattr(publish_pipeline.publish_cap, "refund_cap", nothing)
    monkeypatch.setattr(publish_pipeline.provider_ops, "resolve_permit", nothing)
    monkeypatch.setattr(publish_pipeline, "_say_outcome", nothing)
    return log


class TestTheBestEffortDestroy:
    async def test_it_destroys_the_ref_the_story_carries(self):
        transit = _Transit()
        await _destroy_transit_best_effort(_ctx(), transit)
        assert transit.calls == [("ws/x/t1", "image")]

    async def test_no_ref_is_a_no_op(self):
        transit = _Transit()
        await _destroy_transit_best_effort(_ctx(ref=None), transit)
        assert transit.calls == []

    async def test_a_destroy_error_is_logged_and_swallowed(self, caplog):
        transit = _Transit(raises=True)
        with caplog.at_level(logging.WARNING, logger=publish_pipeline.__name__):
            await _destroy_transit_best_effort(_ctx(), transit)
        assert transit.calls == [("ws/x/t1", "image")]
        assert "the sweep will reap it" in caplog.text


class TestAFailedStoryLosesItsCopy:
    async def test_fail_destroys_after_its_transaction(self, world):
        transit = _Transit()

        async def destroy(ref, *, media_kind):
            world.append("destroy")

        transit.destroy = destroy
        out = await _fail_terminal(
            _Uow(),
            _ctx(),
            op_id=None,
            exc=DriveMediaTooLarge("too large"),
            transit=transit,
        )
        assert out == FAILED
        assert world == ["finalize:failed", "destroy"], "after the commit"

    async def test_a_destroy_error_does_not_unfail(self, world):
        transit = _Transit(raises=True)
        out = await _fail_terminal(
            _Uow(),
            _ctx(),
            op_id=None,
            exc=DriveMediaTooLarge("too large"),
            transit=transit,
        )
        assert out == FAILED and world == ["finalize:failed"]
        assert transit.calls == [("ws/x/t1", "image")]

    async def test_a_fetch_rung_failure_has_nothing_to_destroy(self, world):
        transit = _Transit()
        await _fail_terminal(
            _Uow(),
            _ctx(ref=None),
            op_id=None,
            exc=DriveMediaTooLarge("too large"),
            transit=transit,
        )
        assert transit.calls == []


class TestAJobMeetingATerminalStory:
    """The give-up's job: the intent is already terminal when the worker
    loads it. It destroys the copy, finalizes `cancelled`, and calls nothing
    else — no meta, no upload."""

    @pytest.fixture
    def loaded(self, world, monkeypatch):
        holder: dict = {}

        async def _load(uow, job):
            return holder["ctx"]

        monkeypatch.setattr(publish_pipeline, "_load", _load)
        monkeypatch.setattr(publish_pipeline, "unit_of_work", lambda *a, **k: _Uow())
        return holder

    async def _run(self, transit):
        return await publish_pipeline.run_publish_pipeline(
            dict(JOB),
            engine=None,
            meta=None,
            transit=transit,
            media_fetch=None,
        )

    @pytest.mark.parametrize("state", ["cancelled", "failed", "posted", "expired"])
    async def test_it_destroys_then_finalizes(self, loaded, world, state):
        loaded["ctx"] = _ctx(state=state)
        transit = _Transit()

        async def destroy(ref, *, media_kind):
            world.append(f"destroy:{ref}")

        transit.destroy = destroy
        assert await self._run(transit) == CANCELLED
        assert world == ["destroy:ws/x/t1", "finalize:cancelled"]

    async def test_a_destroy_error_still_finalizes(self, loaded, world):
        loaded["ctx"] = _ctx(state="cancelled")
        transit = _Transit(raises=True)
        assert await self._run(transit) == CANCELLED
        assert world == ["finalize:cancelled"], "no error loop: the job ends"

    async def test_a_review_required_story_keeps_its_copy(self, loaded, world):
        """A retry re-enters at its step with the asset it carries."""
        loaded["ctx"] = _ctx(state="review_required")
        transit = _Transit()
        assert await self._run(transit) == CANCELLED
        assert transit.calls == [] and world == ["finalize:cancelled"]
