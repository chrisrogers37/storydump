"""The pipeline context's discriminators, pinned without a database.

`fetch_failed_before` decides whether a Meta 9004 ("could not be fetched") is
a first fetch losing the race (retried) or the file's own answer (terminal,
and the media locked). It counts recorded 9004 outcomes — and, since the
review card's retry (2026-09-12) uploads a FRESH asset, only this episode's:
ops created before the intent's current `entered_state_at` belong to an
earlier run and say nothing about the new upload.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.services.target.publish_pipeline import FETCH_FAILED_CODE, _Ctx

T1 = datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc)


def _ctx(ops, *, entered_state_at=T1):
    return _Ctx(
        {"id": "j", "workspace_id": "ws"},
        {"id": "i", "entered_state_at": entered_state_at},
        ops,
    )


def _op(
    state="failed", *, error=FETCH_FAILED_CODE, created_at=None, kind="container_create"
):
    return {
        "id": "op",
        "op_kind": kind,
        "state": state,
        "generation": 1,
        "response_ref": {"v": 1, "error": error},
        "created_at": created_at,
    }


class TestTheFirstFetchDiscriminatorCountsThisEpisodeOnly:
    def test_a_9004_in_this_episode_counts(self):
        assert _ctx([_op(created_at=T1 + timedelta(seconds=5))]).fetch_failed_before(
            "container_create"
        )

    def test_a_9004_from_an_earlier_episode_does_not(self):
        assert not _ctx(
            [_op(created_at=T1 - timedelta(minutes=30))]
        ).fetch_failed_before("container_create")

    def test_without_timestamps_the_recorded_outcome_alone_decides(self):
        """Rows without `created_at` (older fixtures, a context built by hand)
        and a context without `entered_state_at` count as this episode."""
        assert _ctx([_op()]).fetch_failed_before("container_create")
        assert _ctx(
            [_op(created_at=T1 - timedelta(days=1))], entered_state_at=None
        ).fetch_failed_before("container_create")

    def test_only_a_recorded_9004_counts_never_a_generation(self):
        assert not _ctx([_op(error=9999, created_at=T1)]).fetch_failed_before(
            "container_create"
        )
        assert not _ctx([_op(state="ambiguous", created_at=T1)]).fetch_failed_before(
            "container_create"
        )
        assert not _ctx([_op(kind="publish", created_at=T1)]).fetch_failed_before(
            "container_create"
        )
