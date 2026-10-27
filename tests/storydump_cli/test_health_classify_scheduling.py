from __future__ import annotations

import json

import pytest

from storydump_cli.health_classify.scheduling import (
    HEALTHY,
    NO_SIGNAL,
    STALLED,
    UNREACHABLE,
    WORKER_DOWN,
    WORKER_UNKNOWN,
    classify,
)


T = 600


#: A worker axis that is provably alive — what a current app serves (#1120).
#: Kept as the DEFAULT so the cursor-axis tests below go on testing the cursor:
#: without it every case would answer `UNREACHABLE`, which is correct for a
#: payload lacking the worker block and is covered by its own test.
#: The age is a healthy one against the 60 s recurring beat — well inside
#: `DEFAULT_WORKER_STALE_S`, which is what "provably alive" has to mean here.
ALIVE_WORKER = {
    "succeeded_ever": 78,
    "last_success_age_seconds": 45,
    "overdue_ready": 0,
    "max_overdue_seconds": None,
}


def body(stalled=0, active=0, lag=None, worker=ALIVE_WORKER):
    """The endpoint payload. `worker=None` omits the worker block."""
    data = {"stalled": stalled, "accounts_active": active, "max_lag_seconds": lag}
    if worker is not None:
        data["worker"] = worker
    return json.dumps(data)


class TestNothingToSeeIsNotTheSameAsNothingWrong:
    """The distinction the whole module exists for: *nothing is late* and
    *nothing EXISTS to be late* have opposite remedies and must never render the
    same."""

    def test_an_empty_estate_is_no_signal_and_never_healthy(self):
        v = classify(200, body(stalled=0, active=0, lag=None), threshold_s=T)
        assert v.state == NO_SIGNAL
        assert v.state != HEALTHY

    def test_the_same_numbers_with_destinations_present_are_healthy(self):
        # `stalled: 0, max_lag_seconds: null` is byte-identical in both cases.
        # ONLY `accounts_active` separates them, which is why it is in the
        # payload at all.
        v = classify(200, body(stalled=0, active=3, lag=None), threshold_s=T)
        assert v.state == HEALTHY


class TestAMalformedAnswerIsNeverAHealthyOne:
    """The lenient spelling — `data.get("stalled", 0)` — turns a broken
    instrument into a clean bill of health. That is this classifier's own failure
    mode, one layer inward."""

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "not json",
            "[]",
            '"a string"',
            "{}",
            '{"stalled": 0}',
            '{"accounts_active": 1}',
            '{"stalled": "0", "accounts_active": 1, "max_lag_seconds": null}',
            '{"stalled": 0, "accounts_active": null, "max_lag_seconds": null}',
            '{"stalled": 0, "accounts_active": 1, "max_lag_seconds": "700"}',
            '{"stalled": true, "accounts_active": 1, "max_lag_seconds": null}',
        ],
    )
    def test_every_malformed_shape_is_unreachable(self, raw):
        assert classify(200, raw, threshold_s=T).state == UNREACHABLE

    @pytest.mark.parametrize("status", [0, 404, 500, 502, 503])
    def test_every_non_200_is_unreachable(self, status):
        assert classify(status, body(active=5), threshold_s=T).state == UNREACHABLE


class TestTheThreshold:
    def test_a_lag_at_the_threshold_is_not_yet_stalled(self):
        assert classify(200, body(1, 5, T), threshold_s=T).state == HEALTHY

    def test_one_second_past_it_is(self):
        assert classify(200, body(1, 5, T + 1), threshold_s=T).state == STALLED

    def test_a_null_lag_with_destinations_is_healthy(self):
        assert classify(200, body(0, 5, None), threshold_s=T).state == HEALTHY


def test_a_dead_worker_is_reported_even_when_no_destination_exists():
    """THE test this axis exists for.

    Zero active destinations makes the cursor axis answer `no-signal` — true,
    and the same answer whether the worker is healthy or dead. A stale worker
    here must surface as an outage, not as the standing "nothing to watch"
    notice.
    """
    verdict = classify(
        200,
        body(active=0, worker={**ALIVE_WORKER, "last_success_age_seconds": 90000}),
        threshold_s=T,
    )

    assert verdict.state == WORKER_DOWN, verdict
    assert verdict.state != NO_SIGNAL


class TestTheWorkerAxisIsNotAllowedToReadAsHealthyWhenItCannotTell:
    """Every one of these is a way the axis could quietly answer "fine".

    The instrument was built because instruments over empty populations return
    their reassuring value. It must not be an instance of its own defect, so
    each blind condition gets a state of its own here.
    """

    @pytest.mark.parametrize(
        "stalled,active,lag",
        [(0, 0, 0), (0, 1, 0), (0, 5, 0), (2, 5, 9999)],
        ids=["none-active", "one-active", "five-active", "a-stall"],
    )
    def test_a_reading_without_the_worker_block_is_unreachable_never_healthy(
        self, stalled, active, lag
    ):
        """The route always serves the block, so a reading without it is a broken
        instrument. With a destination active, the cursor axis alone would
        answer `HEALTHY` and the worker axis would be gone without a word;
        with none, `WORKER_UNKNOWN` would page nobody. With a stall the
        instrument still comes first: read as `STALLED`, it would page one poll
        sooner and drop the worker axis without a word."""
        v = classify(
            200,
            body(stalled=stalled, active=active, lag=lag, worker=None),
            threshold_s=T,
        )
        assert v.state == UNREACHABLE
        assert v.detail == "the endpoint does not serve a worker block"

    def test_a_database_where_nothing_has_ever_run_is_unknown(self):
        """No baseline and no backlog. Genuinely unknowable — and the reading
        is identical to a healthy idle estate, which is why it needs its own
        state rather than a large invented age."""
        v = classify(
            200,
            body(
                active=0,
                worker={
                    **ALIVE_WORKER,
                    "succeeded_ever": 0,
                    "last_success_age_seconds": None,
                },
            ),
            threshold_s=T,
        )
        assert v.state == WORKER_UNKNOWN

    def test_a_worker_block_that_is_not_an_object_is_unreachable(self):
        """Distinct from the mistyped-field case below: without it, swapping
        the not-a-dict guard to `absent` survives every test. A block
        that is present and structurally wrong is a BROKEN instrument, which
        must not degrade into the milder "this app does not serve the axis"."""
        v = classify(200, body(active=0, worker="not-an-object"), threshold_s=T)
        assert v.state == UNREACHABLE
        assert v.state != WORKER_UNKNOWN

    def test_a_malformed_worker_block_is_unreachable_never_healthy(self):
        """Same strictness the cursor axis already applies. A broken instrument
        is not a clean bill of health."""
        v = classify(
            200,
            body(active=0, worker={**ALIVE_WORKER, "overdue_ready": "lots"}),
            threshold_s=T,
        )
        assert v.state == UNREACHABLE


class TestTheTwoWorkerSignalsCoverEachOthersBlindSpot:
    """Staleness catches "nothing was ever enqueued"; backlog catches "enqueued
    and never worked". Neither alone catches both."""

    def test_a_backlog_is_a_dead_worker_even_when_a_success_is_recent(self):
        """The stuck-lane shape: other kinds still finish, so the age signal
        stays quiet while due jobs pile up unclaimed. Staleness alone is blind
        here."""
        v = classify(
            200,
            body(
                active=0,
                worker={
                    **ALIVE_WORKER,
                    "last_success_age_seconds": 60,
                    "overdue_ready": 3,
                    "max_overdue_seconds": 4000,
                },
            ),
            threshold_s=T,
        )
        assert v.state == WORKER_DOWN

    def test_a_dead_worker_is_reported_even_while_the_cursor_looks_healthy(self):
        """Closes the bound `scheduling_lag` names about itself: it "cannot see
        a failure PAST the mint". Cursors keep advancing while nothing is
        worked, so a healthy cursor must not certify the worker."""
        v = classify(
            200,
            body(
                active=5,
                lag=0,
                worker={**ALIVE_WORKER, "last_success_age_seconds": 90000},
            ),
            threshold_s=T,
        )
        assert v.state == WORKER_DOWN


def test_a_live_worker_does_not_mask_a_stalled_cursor():
    """Precedence guard. The worker axis is consulted first, so it must not
    swallow the axis that already worked."""
    v = classify(200, body(stalled=2, active=5, lag=9999), threshold_s=T)
    assert v.state == STALLED
