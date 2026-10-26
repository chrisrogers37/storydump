"""When a refused past-post fetch is tried again, and when it parks."""

import pytest

from src.services.target import past_posts_retry
from src.services.target.past_posts_retry import Retry
from src.services.target.work_loop import WorkerConfig

IDENTITY = ("gen-1", 4, "17890000000000011")
RETRY = Retry(attempts=8, base_seconds=30, cap_seconds=900)
CDN = {"v": 1, "kind": "cdn_refused"}


def graph_error(code, subcode=None):
    return {"v": 1, "kind": "graph_error", "code": code, "error_subcode": subcode}


def capture(attempt, *, identity=IDENTITY, refusal=None, reason=None):
    """A stored capture of refused try `attempt` of `identity`."""
    generation, revision, asset = identity
    return {
        "generation_id": generation,
        "observation_revision": revision,
        "selected_asset_id": asset,
        "state": "validation_pending",
        "reason": reason or "download_pending",
        "attempt": attempt,
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
    assert past_posts_retry.parks(refusal, attempt=1, last_refusal=None, retry=RETRY)


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
def test_a_refusal_that_proves_nothing_is_tried_again(refusal):
    assert not past_posts_retry.parks(
        refusal, attempt=1, last_refusal=None, retry=RETRY
    )


def test_a_try_before_the_last_waits_and_the_last_parks():
    """The try's number is the prepare job's attempts: the ceiling parks."""
    tries = [
        past_posts_retry.parks(CDN, attempt=attempt, last_refusal=CDN, retry=RETRY)
        for attempt in range(1, RETRY.attempts + 1)
    ]
    assert tries == [False] * (RETRY.attempts - 1) + [True]


def test_the_last_refusal_is_the_stored_capture_of_the_same_slide():
    assert past_posts_retry.last_refusal(capture(3), IDENTITY) == CDN


@pytest.mark.parametrize(
    "stored",
    [
        capture(3, identity=("gen-1", 5, "17890000000000011")),
        capture(3, identity=("gen-2", 4, "17890000000000011")),
        capture(3, identity=("gen-1", 4, "17890000000000012")),
        capture(3, reason="asset_unavailable"),
        None,
    ],
    ids=[
        "another-observation",
        "another-generation",
        "another-slide",
        "not-a-refused-try",
        "no-capture",
    ],
)
def test_a_capture_of_anything_else_has_no_last_refusal(stored):
    assert past_posts_retry.last_refusal(stored, IDENTITY) is None


def test_a_single_try_parks_at_once():
    assert past_posts_retry.parks(
        None, attempt=1, last_refusal=None, retry=past_posts_retry.ONCE
    )


def test_a_file_instagram_withholds_on_two_reads_parks():
    """A slide whose kind Instagram names but whose link it withholds, on two
    reads in a row, is a file Instagram will not serve."""
    withheld = {
        "v": 1,
        "kind": "no_media_link",
        "media_type": "VIDEO",
        "has_link": False,
    }

    def parks(last_refusal, attempt):
        return past_posts_retry.parks(
            withheld, attempt=attempt, last_refusal=last_refusal, retry=RETRY
        )

    assert (parks(None, 1), parks(withheld, 2), parks(CDN, 2)) == (
        False,
        True,
        False,
    )


@pytest.mark.parametrize(
    "numbers",
    [(0, 30, 900), (8, -1, 900), (8, 60, 30)],
    ids=["no-attempt", "negative-base", "cap-below-base"],
)
def test_a_retry_refuses_numbers_that_cannot_bound_it(numbers):
    attempts, base, cap = numbers
    with pytest.raises(ValueError):
        Retry(attempts=attempts, base_seconds=base, cap_seconds=cap)
