"""The review card's one sentence when a float is spent (plan 03 D6), at the
unit: it names the minutes since the float began when the row carries that
start, and falls back to "several tries" for any start it cannot read —
including a naive/aware mix, which raises `TypeError`, not `ValueError`
(adversarial review of #1306)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.services.target.publish_pipeline import _poison_notice


class _Ctx:
    def __init__(self, counters):
        self._counters = counters

    def counters(self):
        return self._counters


NOW = datetime(2026, 9, 14, 15, 0, tzinfo=timezone.utc)


class TestThePoisonNotice:
    def test_names_the_minutes_since_the_float_began(self):
        ctx = _Ctx({"float_since": (NOW - timedelta(minutes=34)).isoformat()})
        assert "after 34 minutes of trying" in _poison_notice(ctx, NOW)

    def test_a_float_younger_than_a_minute_says_one(self):
        ctx = _Ctx({"float_since": (NOW - timedelta(seconds=20)).isoformat()})
        assert "after 1 minutes of trying" in _poison_notice(ctx, NOW)

    def test_no_start_on_the_row_says_several_tries(self):
        assert "after several tries" in _poison_notice(_Ctx({}), NOW)

    def test_a_start_it_cannot_parse_says_several_tries(self):
        assert "after several tries" in _poison_notice(
            _Ctx({"float_since": "yesterday"}), NOW
        )

    def test_a_naive_clock_against_an_aware_start_says_several_tries(self):
        """`aware - naive` is a TypeError; the notice must not crash the
        poison transaction over the wording of its own sentence."""
        ctx = _Ctx({"float_since": (NOW - timedelta(minutes=5)).isoformat()})
        naive_now = NOW.replace(tzinfo=None)
        assert "after several tries" in _poison_notice(ctx, naive_now)
