"""When a refused past-post fetch is tried again, and when it parks."""

from datetime import datetime, timedelta, timezone

import pytest

from src.services.target import past_posts_retry
from src.services.target.past_posts_retry import Retry, Tried
from src.services.target.work_loop import WorkerConfig

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
IDENTITY = ("gen-1", 4, "17890000000000011")
RETRY = Retry(attempts=8, base_seconds=30, cap_seconds=900)
CDN = {"v": 1, "kind": "cdn_refused"}


def graph_error(code, subcode=None):
    return {"v": 1, "kind": "graph_error", "code": code, "error_subcode": subcode}


def capture(attempt, *, at=NOW, identity=IDENTITY, refusal=None, reason=None):
    """A stored capture of `attempt` failed tries for `identity`."""
    generation, revision, asset = identity
    return {
        "generation_id": generation,
        "observation_revision": revision,
        "selected_asset_id": asset,
        "state": "validation_pending",
        "reason": reason or "download_pending",
        "attempt": attempt,
        "next_attempt_at": at.isoformat(),
        "refusal": refusal or CDN,
    }


def test_the_wait_doubles_from_its_base_to_its_cap():
    assert [RETRY.delay(attempt) for attempt in range(1, 8)] == [
        30,
        60,
        120,
        240,
        480,
        900,
        900,
    ]


def test_the_default_tries_fit_inside_the_late_window():
    """Every default try starts inside the hour a story may post late (F3):
    the waits before the last try sum to less than `planned_late_seconds`."""
    config = WorkerConfig()
    retry = Retry.from_config(config)
    waits = sum(retry.delay(attempt) for attempt in range(1, retry.attempts))
    assert waits < config.planned_late_seconds


@pytest.mark.parametrize(
    "refusal",
    [
        graph_error(100, 33),
        {"v": 1, "kind": "over_byte_cap", "cap": 8000000, "received": 9000000},
    ],
    ids=["graph-says-it-does-not-exist", "a-named-byte-bound"],
)
def test_a_proven_refusal_parks_at_once(refusal):
    decision = past_posts_retry.decide(refusal, Tried(), NOW, RETRY)
    assert (decision.park, decision.attempt, decision.next_attempt_at) == (
        True,
        1,
        None,
    )


@pytest.mark.parametrize(
    "refusal",
    [
        graph_error(100),
        graph_error(10),
        {"v": 1, "kind": "ownership_unproven", "test": "profile"},
        {"v": 1, "kind": "cdn_refused", "statuses": [404, 404]},
        {"v": 1, "kind": "no_media_link", "media_type": "IMAGE", "has_link": False},
        {"v": 1, "kind": "download_budget_spent", "seconds": 240.0},
        {"v": 1, "kind": "unrecorded", "error": "MediaRetryableError"},
        None,
    ],
    ids=[
        "graph-100-alone",
        "graph-permission",
        "owner-unproven",
        "cdn-404",
        "first-missing-link",
        "download-budget-spent",
        "unrecorded",
        "no-evidence",
    ],
)
def test_a_refusal_that_proves_nothing_is_tried_again_after_the_wait(refusal):
    decision = past_posts_retry.decide(refusal, Tried(), NOW, RETRY)
    assert (decision.park, decision.attempt, decision.next_attempt_at) == (
        False,
        1,
        NOW + timedelta(seconds=30),
    )


def test_the_tries_count_on_from_the_stored_capture_of_the_same_slide():
    tried = past_posts_retry.from_capture(capture(3), IDENTITY)
    decision = past_posts_retry.decide(CDN, tried, NOW, RETRY)
    assert (tried.attempts, tried.refusal, tried.next_attempt_at) == (3, CDN, NOW)
    assert (decision.park, decision.attempt, decision.next_attempt_at) == (
        False,
        4,
        NOW + timedelta(seconds=240),
    )


@pytest.mark.parametrize(
    "stored",
    [
        capture(3, identity=("gen-1", 5, "17890000000000011")),
        capture(3, identity=("gen-2", 4, "17890000000000011")),
        capture(3, identity=("gen-1", 4, "17890000000000012")),
        capture(3, reason="asset_unavailable"),
        {**capture(3), "attempt": None},
        None,
    ],
    ids=[
        "another-observation",
        "another-generation",
        "another-slide",
        "not-a-retry",
        "no-count",
        "no-capture",
    ],
)
def test_a_capture_of_anything_else_counts_no_tries(stored):
    assert past_posts_retry.from_capture(stored, IDENTITY) == Tried()


def test_the_last_try_parks_with_its_refusal():
    decision = past_posts_retry.decide(CDN, Tried(attempts=7), NOW, RETRY)
    assert (decision.park, decision.attempt) == (True, 8)


def test_a_single_try_parks_at_once():
    decision = past_posts_retry.decide(None, Tried(), NOW, past_posts_retry.ONCE)
    assert (decision.park, decision.attempt) == (True, 1)


def test_a_file_instagram_withholds_on_two_reads_parks():
    """A slide whose kind Instagram names but whose link it withholds, on two
    reads in a row, is a file Instagram will not serve."""
    withheld = {
        "v": 1,
        "kind": "no_media_link",
        "media_type": "VIDEO",
        "has_link": False,
    }
    once = past_posts_retry.decide(withheld, Tried(), NOW, RETRY)
    twice = past_posts_retry.decide(
        withheld, Tried(attempts=1, refusal=withheld), NOW, RETRY
    )
    after_cdn = past_posts_retry.decide(
        withheld, Tried(attempts=1, refusal=CDN), NOW, RETRY
    )
    assert (once.park, twice.park, twice.attempt, after_cdn.park) == (
        False,
        True,
        2,
        False,
    )


def test_a_story_waits_until_its_next_try_and_no_longer():
    tried = past_posts_retry.from_capture(
        capture(2, at=NOW + timedelta(seconds=60)), IDENTITY
    )
    assert past_posts_retry.waiting(tried, NOW)
    assert not past_posts_retry.waiting(tried, NOW + timedelta(seconds=60))
    assert not past_posts_retry.waiting(Tried(), NOW)


@pytest.mark.parametrize(
    "numbers",
    [(0, 30, 900), (8, -1, 900), (8, 60, 30)],
    ids=["no-attempt", "negative-base", "cap-below-base"],
)
def test_a_retry_refuses_numbers_that_cannot_bound_it(numbers):
    attempts, base, cap = numbers
    with pytest.raises(ValueError):
        Retry(attempts=attempts, base_seconds=base, cap_seconds=cap)
