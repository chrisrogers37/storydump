"""Tests for scheduler loop token refresh tick."""

import pytest
from unittest.mock import AsyncMock, Mock, patch

from src.exceptions.instagram import TokenRevokedError


@pytest.mark.unit
class TestTokenRefreshTick:
    """Test the _token_refresh_tick function in the scheduler loop."""

    @pytest.fixture
    def mock_token_refresh_service(self):
        service = Mock()
        service.refresh_all_instagram_tokens = AsyncMock()
        service.cleanup_transactions = Mock()
        return service

    @pytest.mark.asyncio
    async def test_tick_calls_refresh_all(self, mock_token_refresh_service):
        """Test that the tick calls refresh_all_instagram_tokens."""
        mock_token_refresh_service.refresh_all_instagram_tokens.return_value = {
            "refreshed": 1,
            "failed": 0,
            "skipped": 2,
        }

        from src.services.core.loops.scheduler_loop import _token_refresh_tick

        await _token_refresh_tick(mock_token_refresh_service)

        mock_token_refresh_service.refresh_all_instagram_tokens.assert_awaited_once()
        mock_token_refresh_service.cleanup_transactions.assert_called_once()

    @pytest.mark.asyncio
    async def test_tick_handles_revoked_error(self, mock_token_refresh_service):
        """Test that TokenRevokedError is caught without crashing."""
        mock_token_refresh_service.refresh_all_instagram_tokens.side_effect = (
            TokenRevokedError("App deauthorized", error_subcode=458)
        )

        from src.services.core.loops.scheduler_loop import _token_refresh_tick

        # Should not raise
        await _token_refresh_tick(mock_token_refresh_service)

        mock_token_refresh_service.cleanup_transactions.assert_called_once()

    @pytest.mark.asyncio
    async def test_tick_handles_generic_error(self, mock_token_refresh_service):
        """Test that generic exceptions are caught without crashing."""
        mock_token_refresh_service.refresh_all_instagram_tokens.side_effect = (
            RuntimeError("DB connection lost")
        )

        from src.services.core.loops.scheduler_loop import _token_refresh_tick

        # Should not raise
        await _token_refresh_tick(mock_token_refresh_service)

        mock_token_refresh_service.cleanup_transactions.assert_called_once()

    @pytest.mark.asyncio
    async def test_tick_cleanup_even_on_error(self, mock_token_refresh_service):
        """Test cleanup_transactions runs even when refresh fails."""
        mock_token_refresh_service.refresh_all_instagram_tokens.side_effect = Exception(
            "boom"
        )

        from src.services.core.loops.scheduler_loop import _token_refresh_tick

        await _token_refresh_tick(mock_token_refresh_service)

        mock_token_refresh_service.cleanup_transactions.assert_called_once()
