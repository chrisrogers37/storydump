"""Tests for the hourly queue cleanup loop (#560, ordering hardened in #550).

The loop reaps button-bearing rows via the shared reap (expire_sent_row)
BEFORE deleting the never-sent accumulation, so live inline buttons are
stripped instead of orphaned.

The loop runs its cleanup work FIRST and sleeps AFTER (#550), so a redeploy
that SIGKILLs the container during the hour-long sleep can never skip a
cleanup cycle. To exercise one iteration, ``asyncio.sleep`` is mocked to
raise ``asyncio.CancelledError`` on its FIRST call — a ``BaseException`` the
loop's ``except Exception`` handler does not catch, so it breaks the loop
after the work has already run. With the old sleep-before-work shape the
reap/delete would never be reached.
"""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.services.core.loops.queue_cleanup_loop import cleanup_queue_loop

_MODULE = "src.services.core.loops.queue_cleanup_loop"


@pytest.mark.unit
class TestCleanupQueueLoop:
    """Ordering + wiring of reap-then-delete, run before the sleep."""

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
            mock_sleep.side_effect = asyncio.CancelledError()
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
            mock_sleep.side_effect = asyncio.CancelledError()
            with pytest.raises(asyncio.CancelledError):
                await cleanup_queue_loop(queue_repo, bot=bot, history_repo=history_repo)

        mock_expire.assert_not_awaited()
        queue_repo.delete_stale.assert_called_once_with(hours=24)

    @pytest.mark.asyncio
    async def test_cleanup_runs_before_first_sleep(self):
        """The reap/delete work runs before the first sleep can break the loop."""
        queue_repo = Mock()
        queue_repo.get_stale_sent.return_value = []
        queue_repo.delete_stale.return_value = 0
        bot = AsyncMock()
        history_repo = Mock()

        with (
            patch(f"{_MODULE}.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            patch(f"{_MODULE}.expire_sent_row", new_callable=AsyncMock),
        ):
            mock_sleep.side_effect = asyncio.CancelledError()
            with pytest.raises(asyncio.CancelledError):
                await cleanup_queue_loop(queue_repo, bot=bot, history_repo=history_repo)

        # Work ran even though the very first sleep raised — not gated behind it.
        queue_repo.get_stale_sent.assert_called_once_with(hours=24)
        queue_repo.delete_stale.assert_called_once_with(hours=24)
        mock_sleep.assert_awaited_once_with(3600)
