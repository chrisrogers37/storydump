"""Tests for the hourly cloud storage cleanup loop (#550).

The loop runs its cleanup work FIRST and sleeps AFTER, so a redeploy that
SIGKILLs the container during the hour-long sleep can never skip a cleanup
cycle (which would otherwise let orphaned Cloudinary uploads accumulate).
``asyncio.sleep`` is mocked to raise ``asyncio.CancelledError`` on its FIRST
call: with work-before-sleep the cleanup still runs before the loop breaks;
with the old sleep-before-work shape the cleanup would never be reached.
"""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.services.core.loops.cloud_cleanup_loop import cleanup_cloud_storage_loop

_MODULE = "src.services.core.loops.cloud_cleanup_loop"
_REPO = "src.repositories.media_repository.MediaRepository"


@pytest.mark.unit
class TestCleanupCloudStorageLoop:
    """Work-first-then-sleep ordering."""

    @pytest.mark.asyncio
    async def test_cleanup_runs_before_first_sleep(self):
        """Cloudinary + DB cleanup run before the first sleep can break the loop."""
        cloud_service = Mock()
        cloud_service.cleanup_expired.return_value = 0

        with (
            patch(f"{_MODULE}.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            patch(_REPO) as mock_repo_cls,
        ):
            mock_repo_cls.return_value.clear_stale_cloud_info.return_value = 0
            mock_sleep.side_effect = asyncio.CancelledError()
            with pytest.raises(asyncio.CancelledError):
                await cleanup_cloud_storage_loop(cloud_service)

        # Both cleanup steps ran even though the very first sleep raised.
        cloud_service.cleanup_expired.assert_called_once()
        mock_repo_cls.return_value.clear_stale_cloud_info.assert_called_once()

    @pytest.mark.asyncio
    async def test_sleeps_after_cleanup(self):
        """The hourly sleep still fires (once) after the cleanup work."""
        cloud_service = Mock()
        cloud_service.cleanup_expired.return_value = 0

        with (
            patch(f"{_MODULE}.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            patch(_REPO) as mock_repo_cls,
        ):
            mock_repo_cls.return_value.clear_stale_cloud_info.return_value = 0
            mock_sleep.side_effect = asyncio.CancelledError()
            with pytest.raises(asyncio.CancelledError):
                await cleanup_cloud_storage_loop(cloud_service)

        mock_sleep.assert_awaited_once_with(3600)

    @pytest.mark.asyncio
    async def test_cleanup_transactions_run_in_finally(self):
        """The finally-block transaction cleanup is preserved for both resources."""
        cloud_service = Mock()
        cloud_service.cleanup_expired.return_value = 0

        with (
            patch(f"{_MODULE}.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            patch(_REPO) as mock_repo_cls,
        ):
            mock_repo_cls.return_value.clear_stale_cloud_info.return_value = 0
            mock_sleep.side_effect = asyncio.CancelledError()
            with pytest.raises(asyncio.CancelledError):
                await cleanup_cloud_storage_loop(cloud_service)

        cloud_service.cleanup_transactions.assert_called_once()
        mock_repo_cls.return_value.end_read_transaction.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_exception_does_not_kill_loop(self):
        """A cleanup error is swallowed; the loop still reaches the sleep."""
        cloud_service = Mock()
        cloud_service.cleanup_expired.side_effect = RuntimeError("boom")

        with (
            patch(f"{_MODULE}.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            patch(_REPO) as mock_repo_cls,
        ):
            mock_repo_cls.return_value.clear_stale_cloud_info.return_value = 0
            mock_sleep.side_effect = asyncio.CancelledError()
            with pytest.raises(asyncio.CancelledError):
                await cleanup_cloud_storage_loop(cloud_service)

        cloud_service.cleanup_expired.assert_called_once()
        mock_sleep.assert_awaited_once_with(3600)
