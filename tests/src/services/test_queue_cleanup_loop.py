"""Tests for the hourly queue cleanup loop (#560).

The loop reaps button-bearing rows via the shared reap (expire_sent_row)
BEFORE deleting the never-sent accumulation, so live inline buttons are
stripped instead of orphaned.

The loop's ``asyncio.sleep`` sits inside its ``except Exception`` handler, so
the loop is broken with ``asyncio.CancelledError`` (a BaseException the
handler does not catch) rather than the usual StopAsyncIteration.
"""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.services.core.loops.queue_cleanup_loop import cleanup_queue_loop

_MODULE = "src.services.core.loops.queue_cleanup_loop"


def _sleep_break_after_first():
    """An async sleep stub that runs one full iteration, then breaks the loop."""
    call_count = 0

    async def counting_sleep(seconds):
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            raise asyncio.CancelledError()

    return counting_sleep


@pytest.mark.unit
class TestCleanupQueueLoop:
    """Ordering + wiring of reap-then-delete."""

    @pytest.mark.asyncio
    async def test_reaps_sent_rows_then_deletes_stale(self):
        """Button-bearing rows are reaped via expire_sent_row before delete_stale."""
        queue_repo = Mock()
        row = Mock()
        queue_repo.get_stale_sent.return_value = [row]
        queue_repo.delete_stale.return_value = 0
        bot = AsyncMock()
        history_repo = Mock()

        with (
            patch(f"{_MODULE}.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            patch(f"{_MODULE}.expire_sent_row", new_callable=AsyncMock) as mock_expire,
        ):
            mock_sleep.side_effect = _sleep_break_after_first()
            with pytest.raises(asyncio.CancelledError):
                await cleanup_queue_loop(queue_repo, bot=bot, history_repo=history_repo)

        queue_repo.get_stale_sent.assert_called_once_with(hours=24)
        mock_expire.assert_awaited_once_with(
            row, bot=bot, history_repo=history_repo, queue_repo=queue_repo
        )
        queue_repo.delete_stale.assert_called_once_with(hours=24)

    @pytest.mark.asyncio
    async def test_deletes_stale_when_no_sent_rows(self):
        """No button-bearing rows → no reap, but delete_stale still runs."""
        queue_repo = Mock()
        queue_repo.get_stale_sent.return_value = []
        queue_repo.delete_stale.return_value = 3
        bot = AsyncMock()
        history_repo = Mock()

        with (
            patch(f"{_MODULE}.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            patch(f"{_MODULE}.expire_sent_row", new_callable=AsyncMock) as mock_expire,
        ):
            mock_sleep.side_effect = _sleep_break_after_first()
            with pytest.raises(asyncio.CancelledError):
                await cleanup_queue_loop(queue_repo, bot=bot, history_repo=history_repo)

        mock_expire.assert_not_awaited()
        queue_repo.delete_stale.assert_called_once_with(hours=24)
