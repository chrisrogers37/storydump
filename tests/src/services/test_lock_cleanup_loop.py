"""Tests for the hourly lock cleanup loop (#550).

The loop runs its cleanup work FIRST and sleeps AFTER, so a redeploy that
SIGKILLs the container during the hour-long sleep can never skip a cleanup
cycle. To prove the ordering, ``asyncio.sleep`` is mocked to raise
``asyncio.CancelledError`` on its FIRST call: with work-before-sleep the
cleanup still runs before the loop is broken, so the assertions pass; with
the old sleep-before-work shape the cleanup would never be reached.

``CancelledError`` is a ``BaseException`` the loop's ``except Exception``
handler does not catch, so it cleanly breaks the loop after one iteration.
"""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.services.core.loops.lock_cleanup_loop import cleanup_locks_loop

_MODULE = "src.services.core.loops.lock_cleanup_loop"


@pytest.mark.unit
class TestCleanupLocksLoop:
    """Work-first-then-sleep ordering."""

    @pytest.mark.asyncio
    async def test_cleanup_runs_before_first_sleep(self):
        """cleanup_expired_locks runs before the first sleep can break the loop."""
        lock_service = Mock()
        lock_service.cleanup_expired_locks.return_value = 0

        with patch(f"{_MODULE}.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            mock_sleep.side_effect = asyncio.CancelledError()
            with pytest.raises(asyncio.CancelledError):
                await cleanup_locks_loop(lock_service)

        # Cleanup ran even though the very first sleep raised — it is not
        # gated behind the sleep.
        lock_service.cleanup_expired_locks.assert_called_once()

    @pytest.mark.asyncio
    async def test_sleeps_after_cleanup(self):
        """The hourly sleep still fires (once) after the cleanup work."""
        lock_service = Mock()
        lock_service.cleanup_expired_locks.return_value = 5

        with patch(f"{_MODULE}.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            mock_sleep.side_effect = asyncio.CancelledError()
            with pytest.raises(asyncio.CancelledError):
                await cleanup_locks_loop(lock_service)

        mock_sleep.assert_awaited_once_with(3600)

    @pytest.mark.asyncio
    async def test_cleanup_transactions_runs_in_finally(self):
        """The finally-block transaction cleanup is preserved."""
        lock_service = Mock()
        lock_service.cleanup_expired_locks.return_value = 0

        with patch(f"{_MODULE}.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            mock_sleep.side_effect = asyncio.CancelledError()
            with pytest.raises(asyncio.CancelledError):
                await cleanup_locks_loop(lock_service)

        lock_service.cleanup_transactions.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_exception_does_not_kill_loop(self):
        """A cleanup error is swallowed; the loop still reaches the sleep."""
        lock_service = Mock()
        lock_service.cleanup_expired_locks.side_effect = RuntimeError("boom")

        with patch(f"{_MODULE}.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            mock_sleep.side_effect = asyncio.CancelledError()
            # The loop must not surface the RuntimeError — it is caught and
            # the loop proceeds to the sleep (which is what breaks it here).
            with pytest.raises(asyncio.CancelledError):
                await cleanup_locks_loop(lock_service)

        lock_service.cleanup_expired_locks.assert_called_once()
        mock_sleep.assert_awaited_once_with(3600)
