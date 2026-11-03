"""The publish pipeline's notices, at the unit. The review card's one
sentence when a float is spent names the minutes since the float began when
the row carries that start, and falls back to "several tries" for any start
it cannot read — including a naive/aware mix, which raises `TypeError`, not
`ValueError` (#1306). The operators' drift message masks an object id in
Meta's words, as a card line does."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.services.target.publish_pipeline import _drift_notice, _poison_notice


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


#: An invented object id of the length Instagram's run to.
OBJECT_ID = "12345678901234567"


class TestTheDriftNotice:
    """Meta's words in the operators' message may name the object they refer
    to: the record keeps the id, and the message says `<id>`, as a card line
    does."""

    @pytest.mark.parametrize(
        "meta",
        [
            {
                "type": "MetaRetryableError",
                "code": 100,
                "subcode": 33,
                "message": f"meta error 100/33: Object with ID '{OBJECT_ID}'"
                " does not exist",
            },
            {
                "type": "ContainerDead",
                "status_code": "ERROR",
                "status": f"Error: container {OBJECT_ID} could not be processed",
            },
        ],
        ids=["message", "status"],
    )
    def test_an_object_id_in_metas_words_is_masked(self, meta):
        key, text = _drift_notice(
            SimpleNamespace(intent_id="intent-1"), {"meta": meta, "ours": {}}
        )
        assert OBJECT_ID not in text and OBJECT_ID not in key
        assert "Meta: " in text and "<id>" in text
