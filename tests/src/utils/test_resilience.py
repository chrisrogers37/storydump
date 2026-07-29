"""Tests for ``src.utils.resilience.telegram_edit_with_retry``.

Pins the retry classifier's contract:
- ``BadRequest`` (permanent — e.g. "Message is not modified"): not retried,
  returns ``None``.
- ``RetryAfter`` (flood control): the AIORateLimiter now OWNS rate + RetryAfter
  retry (one layer, #686b), so this wrapper does NOT retry/blocking-sleep on it
  — a RetryAfter that still escapes the limiter is terminal here and returns
  ``None`` (never re-raised: callers rely on None-on-failure).
- ``TimedOut`` / ``NetworkError`` (network transients, not owned by the rate
  limiter): keep their bounded-retry behavior.

``BadRequest`` subclasses ``NetworkError`` in python-telegram-bot, so the
permanent branch must be classified first — misordering it is the regression
behind the #682 flood-control storm.
"""

from unittest.mock import AsyncMock, patch

import pytest
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut

from src.utils.resilience import telegram_edit_with_retry


@pytest.fixture(autouse=True)
def _no_sleep():
    """Backoff sleeps are real time — no-op them to keep tests fast."""
    with patch("src.utils.resilience.asyncio.sleep", new_callable=AsyncMock):
        yield


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
    async def test_retry_after_is_terminal_no_retry(self):
        """AIORateLimiter owns rate + RetryAfter retry now (#686b). A RetryAfter
        that still escapes it is terminal here: one attempt, return None — never
        blocking-sleep (it would stall the caller's op-lock) and never re-raise
        (callers rely on None-on-failure)."""
        edit = AsyncMock(side_effect=RetryAfter(18))

        result = await telegram_edit_with_retry(edit)

        assert result is None
        assert edit.await_count == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "transient",
        [TimedOut(), NetworkError("connection dropped")],
        ids=["timed_out", "network_error"],
    )
    async def test_network_transient_retries_then_none(self, transient):
        edit = AsyncMock(side_effect=transient)

        result = await telegram_edit_with_retry(edit)

        assert result is None
        assert edit.await_count == 3

    @pytest.mark.asyncio
    async def test_transient_then_success_returns_result(self):
        edit = AsyncMock(side_effect=[TimedOut(), "edited-message"])

        result = await telegram_edit_with_retry(edit)

        assert result == "edited-message"
        assert edit.await_count == 2
