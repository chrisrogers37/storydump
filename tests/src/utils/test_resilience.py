"""Tests for ``src.utils.resilience.telegram_edit_with_retry``.

Pins the retry classifier's contract: permanent Telegram rejections
(``BadRequest`` — e.g. "Message is not modified") are not retried, transient
network failures (``TimedOut`` / ``NetworkError``) keep their bounded-retry
behavior, and ``RetryAfter`` gets a single attempt with no sleeps — the
Application's AIORateLimiter is the single owner of rate pacing and
RetryAfter retries (#686b), so one surfacing here is already past the
limiter's retries. ``BadRequest`` subclasses ``NetworkError``
in python-telegram-bot, so the permanent branch must be classified first —
misordering it is the regression behind the #682 flood-control storm.
"""

from unittest.mock import AsyncMock, patch

import pytest
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut

from src.utils.resilience import telegram_edit_with_retry


@pytest.fixture(autouse=True)
def _no_sleep():
    """Backoff sleeps are real time — no-op them to keep tests fast."""
    with patch("src.utils.resilience.asyncio.sleep", new_callable=AsyncMock) as mock:
        yield mock


@pytest.mark.unit
class TestTelegramEditWithRetry:
    @pytest.mark.asyncio
    async def test_success_first_try_returns_result(self):
        edit = AsyncMock(return_value="edited-message")

        result = await telegram_edit_with_retry(edit)

        assert result == "edited-message"
        assert edit.await_count == 1

    @pytest.mark.asyncio
    async def test_bad_request_is_permanent_no_retry(self):
        """'Message is not modified' can never succeed on retry — one attempt only."""
        edit = AsyncMock(
            side_effect=BadRequest(
                "Message is not modified: specified new message content and "
                "reply markup are exactly the same as a current content and "
                "reply markup of the message"
            )
        )

        result = await telegram_edit_with_retry(edit)

        assert result is None
        assert edit.await_count == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "transient",
        [TimedOut(), NetworkError("connection dropped")],
        ids=["timed_out", "network_error"],
    )
    async def test_transient_retries_then_none(self, transient):
        edit = AsyncMock(side_effect=transient)

        result = await telegram_edit_with_retry(edit)

        assert result is None
        assert edit.await_count == 3

    @pytest.mark.asyncio
    async def test_retry_after_single_attempt_no_sleep(self, _no_sleep):
        """RetryAfter is not retried (or slept on) at this layer. The
        AIORateLimiter below owns rate pacing and RetryAfter retries; one
        surfacing here means the limiter's retries are exhausted (or the
        limiter is disabled), and a second retry ladder here would stack
        its blocking waits under the caller's operation lock (#686b)."""
        edit = AsyncMock(side_effect=RetryAfter(7))

        result = await telegram_edit_with_retry(edit)

        assert result is None
        assert edit.await_count == 1
        _no_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_transient_then_success_returns_result(self):
        edit = AsyncMock(side_effect=[TimedOut(), "edited-message"])

        result = await telegram_edit_with_retry(edit)

        assert result == "edited-message"
        assert edit.await_count == 2
