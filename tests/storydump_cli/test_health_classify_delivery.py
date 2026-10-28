"""What one `/health/delivery` reading means.

`storydump health` and the scheduling monitor judge delivery with this one
verdict, so the CLI is not well exactly when the monitor would page. How the
monitor repeats and recovers is pinned in `tests/scripts/test_scheduling_monitor.py`.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def delivery():
    """The module under test, imported per test, so a missing or broken module
    fails each test by name instead of failing collection for every shard."""
    from storydump_cli.health_classify import delivery

    return delivery


def reading(**over):
    """A quiet, readable hour, overridden per test. Every key the route
    publishes is present, so a test of one key never trips another's check."""
    base = {
        "window_seconds": 3600,
        "sent_in_window": 40,
        "failed_or_ambiguous": 0,
        "by_class": {},
    }
    return json.dumps({**base, **over})


REFUSED = {"refused": {"rows": 1, "alerting": 1, "codes": {"400": 1}}}
DEFERRED = {"rate_limited": {"rows": 4, "alerting": 0, "codes": {"429": 4}}}


class TestWhatAReadingSays:
    def test_an_hour_with_no_failure_is_delivering(self, delivery):
        v = delivery.classify(200, reading())
        assert v.state == delivery.DELIVERING
        assert v.detail == "no send failed or is ambiguous in the last hour (40 sent)"

    def test_one_failed_send_is_failing(self, delivery):
        v = delivery.classify(200, reading(failed_or_ambiguous=1, by_class=REFUSED))
        assert v.state == delivery.FAILING
        assert v.detail == (
            "1 of the last hour's sends failed or are ambiguous "
            "(40 sent; refused: 400×1)"
        )

    def test_a_deferral_is_context_never_a_failure(self, delivery):
        both = {
            "ambiguous": {"rows": 2, "alerting": 2, "codes": {"none": 2}},
            **DEFERRED,
        }
        v = delivery.classify(200, reading(failed_or_ambiguous=2, by_class=both))
        assert v.state == delivery.FAILING
        assert "ambiguous: none×2" in v.detail
        assert "rate_limited" not in v.detail and "429" not in v.detail

    def test_an_hour_of_deferrals_alone_is_delivering(self, delivery):
        v = delivery.classify(200, reading(by_class=DEFERRED))
        assert v.state == delivery.DELIVERING

    def test_a_failure_with_no_class_still_says_so(self, delivery):
        v = delivery.classify(200, reading(failed_or_ambiguous=2))
        assert v.state == delivery.FAILING
        assert v.detail.endswith("(40 sent; no class recorded)")

    def test_a_readable_verdict_carries_its_reading(self, delivery):
        v = delivery.classify(200, reading(sent_in_window=7))
        assert v.payload["sent_in_window"] == 7


class TestAnUnreadableReadingIsNeverAHealthyOne:
    """A broken instrument is not a clean bill of health: every reading the
    classifier cannot read is `unreachable`, and the CLI and the monitor both
    say so instead of passing over it."""

    @pytest.mark.parametrize("status", [404, 500, 503])
    def test_every_non_200_is_unreachable(self, delivery, status):
        v = delivery.classify(status, reading())
        assert v.state == delivery.UNREACHABLE
        assert v.detail == f"HTTP {status}"

    def test_a_transport_failure_names_itself(self, delivery):
        v = delivery.classify(0, "  connection refused  ")
        assert v.state == delivery.UNREACHABLE
        assert v.detail == "connection refused"

    @pytest.mark.parametrize(
        "raw",
        [
            "not json",
            json.dumps([1, 2]),
            json.dumps({"window_seconds": 3600, "failed_or_ambiguous": 0}),
            reading(sent_in_window=None),
            reading(sent_in_window=True),
            reading(failed_or_ambiguous=-1),
            reading(failed_or_ambiguous="3"),
            reading(by_class=[]),
            json.dumps(
                {"window_seconds": 3600, "sent_in_window": 1, "failed_or_ambiguous": 0}
            ),
        ],
        ids=[
            "not-json",
            "not-an-object",
            "no-sent-count",
            "null-count",
            "bool-count",
            "negative-count",
            "string-count",
            "by-class-not-an-object",
            "no-by-class",
        ],
    )
    def test_every_malformed_reading_is_unreachable(self, delivery, raw):
        assert delivery.classify(200, raw).state == delivery.UNREACHABLE
