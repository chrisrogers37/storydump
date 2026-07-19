"""Tests for ``src.utils.resilience.telegram_edit_with_retry``.

Pins the retry classifier's contract: permanent Telegram rejections
(``BadRequest`` — e.g. "Message is not modified") are not retried, while
genuinely transient failures (``RetryAfter`` / ``TimedOut`` / ``NetworkError``)
keep their bounded-retry behavior. ``BadRequest`` subclasses ``NetworkError``
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
    @pytest.mark.parametrize(
        "transient",
        [RetryAfter(0), TimedOut(), NetworkError("connection dropped")],
        ids=["retry_after", "timed_out", "network_error"],
    )
    async def test_transient_retries_then_none(self, transient):
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
