"""Tests for media_sync_loop helpers."""

import pytest
from unittest.mock import AsyncMock, Mock

from src.services.core.loops.media_sync_loop import _notify_sync_error


@pytest.mark.unit
class TestNotifySyncError:
    """#541 — loop-level sync failures are deployment-level operational
    alerts and must go to the admin chat, not whichever tenant happens to
    be the env TELEGRAM_CHANNEL_ID."""

    ADMIN_CHAT_ID = -100999000111

    def _make_service(self):
        service = Mock()
        service.admin_chat_id = self.ADMIN_CHAT_ID
        service.bot = AsyncMock()
        return service

    async def test_notifies_admin_chat_not_env_channel(self):
        service = self._make_service()

        await _notify_sync_error(service, "Media Sync Failed")

        service.bot.send_message.assert_called_once()
        kwargs = service.bot.send_message.call_args.kwargs
        assert kwargs["chat_id"] == self.ADMIN_CHAT_ID

    async def test_send_failure_is_suppressed(self):
        """Notification errors must never propagate into the sync loop."""
        service = self._make_service()
        service.bot.send_message.side_effect = RuntimeError("network down")

        await _notify_sync_error(service, "Media Sync Failed")
