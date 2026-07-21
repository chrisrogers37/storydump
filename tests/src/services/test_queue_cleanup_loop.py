"""Tests for the hourly queue cleanup loop (#560, ordering hardened in #550).

The loop reaps button-bearing rows via the shared reap (expire_sent_row),
then sweeps the unstamped accumulation through record_expiry_and_delete
(#687) — an unstamped row may still have delivered a live card (#679/#680),
so every deletion writes the terminal 'expired' history row first and a
late tap degrades to "Expired" instead of the raw "Queue item not found".

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
from src.services.core.queue_reap import reconcile_aged_unconfirmed

_MODULE = "src.services.core.loops.queue_cleanup_loop"


@pytest.mark.unit
class TestCleanupQueueLoop:
    """Ordering + wiring of reap-then-delete, run before the sleep."""

    @pytest.mark.asyncio
    async def test_reaps_sent_rows_then_sweeps_unsent(self):
        """Button-bearing rows are reaped via expire_sent_row; the unstamped
        accumulation is swept via record_expiry_and_delete."""
        queue_repo = Mock()
        row = Mock()
        queue_repo.get_stale_sent.return_value = [row]
        queue_repo.get_stale_unsent.return_value = []
        bot = AsyncMock()
        history_repo = Mock()

        with (
            patch(f"{_MODULE}.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            patch(f"{_MODULE}.asyncio.to_thread", new_callable=AsyncMock),
            patch(f"{_MODULE}.expire_sent_row", new_callable=AsyncMock) as mock_expire,
        ):
            mock_sleep.side_effect = asyncio.CancelledError()
            with pytest.raises(asyncio.CancelledError):
                await cleanup_queue_loop(queue_repo, bot=bot, history_repo=history_repo)

        queue_repo.get_stale_sent.assert_called_once_with(hours=24)
        mock_expire.assert_awaited_once_with(
            row, bot=bot, history_repo=history_repo, queue_repo=queue_repo
        )
        queue_repo.get_stale_unsent.assert_called_once_with(hours=24)

    @pytest.mark.asyncio
    async def test_unsent_stale_rows_get_history_before_delete(self):
        """#687: every stale unstamped row goes through record_expiry_and_delete
        — never a raw hard-delete. A delivered-but-unstamped card (#679/#680)
        then shows "Expired" on tap instead of "Queue item not found"."""
        queue_repo = Mock()
        queue_repo.get_stale_sent.return_value = []
        row_a = Mock()
        row_b = Mock()
        queue_repo.get_stale_unsent.return_value = [row_a, row_b]
        bot = AsyncMock()
        history_repo = Mock()

        with (
            patch(f"{_MODULE}.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            patch(f"{_MODULE}.asyncio.to_thread", new_callable=AsyncMock),
            patch(f"{_MODULE}.record_expiry_and_delete") as mock_record,
        ):
            mock_record.return_value = True
            mock_sleep.side_effect = asyncio.CancelledError()
            with pytest.raises(asyncio.CancelledError):
                await cleanup_queue_loop(queue_repo, bot=bot, history_repo=history_repo)

        assert mock_record.call_count == 2
        mock_record.assert_any_call(
            row_a, history_repo=history_repo, queue_repo=queue_repo
        )
        mock_record.assert_any_call(
            row_b, history_repo=history_repo, queue_repo=queue_repo
        )

    @pytest.mark.asyncio
    async def test_sweeps_unsent_when_no_sent_rows(self):
        """No button-bearing rows → no reap, but the unsent sweep still runs."""
        queue_repo = Mock()
        queue_repo.get_stale_sent.return_value = []
        queue_repo.get_stale_unsent.return_value = []
        bot = AsyncMock()
        history_repo = Mock()

        with (
            patch(f"{_MODULE}.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            patch(f"{_MODULE}.asyncio.to_thread", new_callable=AsyncMock),
            patch(f"{_MODULE}.expire_sent_row", new_callable=AsyncMock) as mock_expire,
        ):
            mock_sleep.side_effect = asyncio.CancelledError()
            with pytest.raises(asyncio.CancelledError):
                await cleanup_queue_loop(queue_repo, bot=bot, history_repo=history_repo)

        mock_expire.assert_not_awaited()
        queue_repo.get_stale_unsent.assert_called_once_with(hours=24)

    @pytest.mark.asyncio
    async def test_cleanup_runs_before_first_sleep(self):
        """The reap/sweep work runs before the first sleep can break the loop."""
        queue_repo = Mock()
        queue_repo.get_stale_sent.return_value = []
        queue_repo.get_stale_unsent.return_value = []
        bot = AsyncMock()
        history_repo = Mock()

        with (
            patch(f"{_MODULE}.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            patch(f"{_MODULE}.asyncio.to_thread", new_callable=AsyncMock),
            patch(f"{_MODULE}.expire_sent_row", new_callable=AsyncMock),
        ):
            mock_sleep.side_effect = asyncio.CancelledError()
            with pytest.raises(asyncio.CancelledError):
                await cleanup_queue_loop(queue_repo, bot=bot, history_repo=history_repo)

        # Work ran even though the very first sleep raised — not gated behind it.
        queue_repo.get_stale_sent.assert_called_once_with(hours=24)
        queue_repo.get_stale_unsent.assert_called_once_with(hours=24)
        mock_sleep.assert_awaited_once_with(3600)

    @pytest.mark.asyncio
    async def test_aged_reconcile_runs_offloaded(self):
        """PR4: the aged sent_unconfirmed reconcile is dispatched via
        asyncio.to_thread — its synchronous DB pass runs OFF the shared event
        loop so it can never block other tenants' callbacks (#682/#573)."""
        queue_repo = Mock()
        queue_repo.get_stale_sent.return_value = []
        queue_repo.get_stale_unsent.return_value = []
        bot = AsyncMock()
        history_repo = Mock()

        with (
            patch(f"{_MODULE}.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            patch(
                f"{_MODULE}.asyncio.to_thread", new_callable=AsyncMock
            ) as mock_to_thread,
        ):
            mock_sleep.side_effect = asyncio.CancelledError()
            with pytest.raises(asyncio.CancelledError):
                await cleanup_queue_loop(queue_repo, bot=bot, history_repo=history_repo)

        # Offloaded (not called inline) and pointed at the bounded reconcile.
        mock_to_thread.assert_awaited_once_with(reconcile_aged_unconfirmed)
