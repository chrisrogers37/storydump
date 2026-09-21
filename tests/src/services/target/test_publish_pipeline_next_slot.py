"""`_next_slot` returns the pair the three deferral sites need.

The helper existed with no caller while its expression sat inlined verbatim at
three sites, each of which kept `slot` separately because the card line
downstream is conditional on it (#1325 audit, TD-A6). It now returns
`(slot, run_at)`, and this pins both halves: no stamped slot yields
`(None, now + first rung)`, a stamped slot yields `(slot, slot)`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.services.target.publish_pipeline import _next_slot

NOW = datetime(2026, 9, 14, 15, 0, tzinfo=timezone.utc)
BACKOFF = (90, 300, 900)


class _Ctx:
    def __init__(self, next_slot_at):
        self.intent = {"next_slot_at": next_slot_at}


def _now():
    return NOW


class TestTheDeferralTarget:
    def test_no_stamped_slot_waits_one_backoff_rung(self):
        slot, run_at = _next_slot(_Ctx(None), _now, BACKOFF)
        assert slot is None
        assert run_at == NOW + timedelta(seconds=BACKOFF[0])

    def test_a_stamped_slot_ahead_is_both_halves_of_the_pair(self):
        stamped = NOW + timedelta(hours=3)
        slot, run_at = _next_slot(_Ctx(stamped), _now, BACKOFF)
        assert slot == stamped
        assert run_at == stamped

    def test_a_stamped_slot_already_past_is_not_a_slot(self):
        # `_slot_at` only answers with a slot that is still ahead; a stale
        # stamp waits a rung and says nothing on the card.
        slot, run_at = _next_slot(_Ctx(NOW - timedelta(minutes=1)), _now, BACKOFF)
        assert slot is None
        assert run_at == NOW + timedelta(seconds=BACKOFF[0])
