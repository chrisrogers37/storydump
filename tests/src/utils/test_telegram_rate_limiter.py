"""Tests for ``SmokeAlarmRateLimiter`` (#686b).

Pins the one behaviour the subclass adds over PTB's ``AIORateLimiter``: a
throttled warning when the OVERALL (cross-tenant) bucket — not the per-group
bucket — is the binding constraint (the multi-user ceiling smoke alarm). The
limiter must otherwise behave exactly like the base class (still run the
request), and observability must never break a send.
"""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.utils.telegram_rate_limiter import (
    OVERALL_SATURATION_MARKER,
    SmokeAlarmRateLimiter,
)


def _limiter():
    # Real buckets with headroom so the actual `async with` acquisitions in
    # _run_request never block; saturation is simulated via has_capacity below.
    return SmokeAlarmRateLimiter(
        overall_max_rate=30, overall_time_period=1, max_retries=0
    )


async def _run(limiter, *, chat=True, group=False, allow_paid_broadcast=False):
    callback = AsyncMock(return_value="edited")
    result = await limiter._run_request(
        chat=chat,
        group=group,
        allow_paid_broadcast=allow_paid_broadcast,
        callback=callback,
        args=(),
        kwargs={},
    )
    return result, callback


@pytest.mark.unit
class TestSmokeAlarmRateLimiter:
    @pytest.mark.asyncio
    async def test_alarms_when_overall_bucket_saturated(self):
        limiter = _limiter()
        limiter._base_limiter.has_capacity = Mock(return_value=False)  # saturated

        with patch("src.utils.telegram_rate_limiter.logger.warning") as warn:
            result, callback = await _run(limiter)

        # The request still ran (limiter is transparent) ...
        assert result == "edited"
        callback.assert_awaited_once()
        # ... and the overall-saturation smoke alarm fired, with its marker.
        assert warn.called
        assert OVERALL_SATURATION_MARKER in warn.call_args.args[0]

    @pytest.mark.asyncio
    async def test_no_alarm_when_overall_bucket_has_capacity(self):
        limiter = _limiter()
        limiter._base_limiter.has_capacity = Mock(return_value=True)  # healthy

        with patch("src.utils.telegram_rate_limiter.logger.warning") as warn:
            result, callback = await _run(limiter)

        assert result == "edited"
        callback.assert_awaited_once()
        warn.assert_not_called()

    @pytest.mark.asyncio
    async def test_alarm_is_throttled_within_window(self):
        """A sustained burst must not flood the log with its own alarm."""
        limiter = _limiter()
        limiter._base_limiter.has_capacity = Mock(return_value=False)

        with patch("src.utils.telegram_rate_limiter.logger.warning") as warn:
            await _run(limiter)
            await _run(limiter)
            await _run(limiter)

        assert warn.call_count == 1

    @pytest.mark.asyncio
    async def test_no_alarm_for_non_chat_request(self):
        """No chat_id ⇒ the overall bucket doesn't apply ⇒ no alarm."""
        limiter = _limiter()
        limiter._base_limiter.has_capacity = Mock(return_value=False)

        with patch("src.utils.telegram_rate_limiter.logger.warning") as warn:
            result, callback = await _run(limiter, chat=False)

        assert result == "edited"
        callback.assert_awaited_once()
        warn.assert_not_called()

    @pytest.mark.asyncio
    async def test_has_capacity_error_never_breaks_the_send(self):
        """Observability is best-effort: if the capacity probe raises, the
        request must still go through and no alarm is logged."""
        limiter = _limiter()
        limiter._base_limiter.has_capacity = Mock(side_effect=RuntimeError("no loop"))

        with patch("src.utils.telegram_rate_limiter.logger.warning") as warn:
            result, callback = await _run(limiter)

        assert result == "edited"
        callback.assert_awaited_once()
        warn.assert_not_called()
