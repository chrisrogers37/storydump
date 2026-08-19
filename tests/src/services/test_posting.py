"""Tests for PostingService.

PostingService is now only responsible for the Google Drive disconnect
alert. The alert is gated on chat_settings.gdrive_alerted_at — fires once
per disconnect event and stays silent until the OAuth reconnect callback
clears the flag. The caller supplies the (rate-limited) bot; the service
holds no bot of its own.
"""

from datetime import datetime, timezone

import pytest
from unittest.mock import AsyncMock, Mock, patch

from src.services.core.posting import PostingService


@pytest.fixture
def posting_service():
    """Create PostingService with mocked dependencies."""
    with patch.object(PostingService, "__init__", lambda self: None):
        service = PostingService()
        service.settings_service = Mock()
        service.service_run_repo = Mock()
        service.service_name = "PostingService"
        return service


def _chat_settings(alerted_at=None):
    """Build a mock ChatSettings with the given gdrive_alerted_at."""
    cs = Mock()
    cs.gdrive_alerted_at = alerted_at
    return cs


@pytest.mark.unit
class TestSendGdriveAuthAlert:
    """send_gdrive_auth_alert behaves as a state-transition notification."""

    @pytest.mark.asyncio
    @patch("src.services.core.posting.settings")
    async def test_sends_and_persists_timestamp_when_flag_null(
        self, mock_settings, posting_service
    ):
        """First auth error in a disconnect event sends the alert and persists."""
        mock_settings.ADMIN_TELEGRAM_CHAT_ID = -100123
        mock_settings.OAUTH_REDIRECT_BASE_URL = "https://example.com"
        posting_service.settings_service.get_settings.return_value = _chat_settings(
            alerted_at=None
        )

        bot = AsyncMock()
        await posting_service.send_gdrive_auth_alert(-100123, bot=bot)

        bot.send_message.assert_called_once()
        call_kwargs = bot.send_message.call_args.kwargs
        assert "Disconnected" in call_kwargs["text"]
        assert "/start" in call_kwargs["text"]

        posting_service.settings_service.set_gdrive_alerted_at.assert_called_once()
        args, _ = posting_service.settings_service.set_gdrive_alerted_at.call_args
        assert args[0] == -100123
        assert isinstance(args[1], datetime)

    @pytest.mark.asyncio
    @patch("src.services.core.posting.settings")
    async def test_skips_send_when_flag_already_set(
        self, mock_settings, posting_service
    ):
        """Second auth error within the same disconnect event is suppressed."""
        mock_settings.ADMIN_TELEGRAM_CHAT_ID = -100123
        mock_settings.OAUTH_REDIRECT_BASE_URL = "https://example.com"
        posting_service.settings_service.get_settings.return_value = _chat_settings(
            alerted_at=datetime(2026, 5, 14, tzinfo=timezone.utc)
        )

        bot = AsyncMock()
        await posting_service.send_gdrive_auth_alert(-100123, bot=bot)

        bot.send_message.assert_not_called()
        posting_service.settings_service.set_gdrive_alerted_at.assert_not_called()

    @pytest.mark.asyncio
    @patch("src.services.core.posting.settings")
    async def test_skips_send_when_no_chat_settings(
        self, mock_settings, posting_service
    ):
        """Unknown chat (no chat_settings row) is silently skipped."""
        mock_settings.ADMIN_TELEGRAM_CHAT_ID = -100123
        mock_settings.OAUTH_REDIRECT_BASE_URL = "https://example.com"
        posting_service.settings_service.get_settings.return_value = None

        bot = AsyncMock()
        await posting_service.send_gdrive_auth_alert(-100123, bot=bot)

        bot.send_message.assert_not_called()
        posting_service.settings_service.set_gdrive_alerted_at.assert_not_called()

    @pytest.mark.asyncio
    @patch("src.services.core.posting.settings")
    async def test_omitting_the_chat_is_a_caller_bug_not_an_admin_grant(
        self, mock_settings, posting_service
    ):
        """#867. This spot previously asserted the opposite — that an omitted
        chat SILENTLY became ADMIN_TELEGRAM_CHAT_ID. That is the F.1 fail-open
        shape wearing a default, and it was undocumented here: the docstring
        described the gating, the state and the bot, and never mentioned that
        an absent id redirected a tenant's alert to the admin chat.

        The parameter is required now, so the mistake cannot be made silently.
        """
        mock_settings.ADMIN_TELEGRAM_CHAT_ID = -100999
        bot = AsyncMock()

        with pytest.raises(TypeError):
            await posting_service.send_gdrive_auth_alert(bot=bot)

        bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    @patch("src.services.core.posting.settings")
    async def test_an_explicitly_absent_chat_never_reaches_the_admin_tenant(
        self, mock_settings, posting_service
    ):
        """The load-bearing half, and the one that goes red on the old code:
        an admin chat IS configured, so under the retired fallback this call
        delivered a tenant alert to it. Now it sends nothing at all."""
        mock_settings.ADMIN_TELEGRAM_CHAT_ID = -100999
        mock_settings.OAUTH_REDIRECT_BASE_URL = "https://example.com"
        posting_service.settings_service.get_settings.return_value = _chat_settings(
            alerted_at=None
        )

        bot = AsyncMock()
        await posting_service.send_gdrive_auth_alert(None, bot=bot)

        bot.send_message.assert_not_called()
        posting_service.settings_service.get_settings.assert_not_called()

    @pytest.mark.asyncio
    @patch("src.services.core.posting.settings")
    async def test_alert_carries_no_oauth_deep_link(
        self, mock_settings, posting_service
    ):
        """The alert never hands out a start link, configured base URL or not (#725).

        The scheduler raises this for a chat, not for a user, so there is no
        member to sign a URL token for; an unsigned link would be a start
        endpoint invocation for a chat_id anyone can read off the message.
        """
        mock_settings.ADMIN_TELEGRAM_CHAT_ID = -100123
        mock_settings.OAUTH_REDIRECT_BASE_URL = "https://example.com"
        posting_service.settings_service.get_settings.return_value = _chat_settings(
            alerted_at=None
        )

        bot = AsyncMock()
        await posting_service.send_gdrive_auth_alert(-100123, bot=bot)

        call_kwargs = bot.send_message.call_args.kwargs
        assert "/auth/google-drive/start" not in call_kwargs["text"]
        assert call_kwargs.get("reply_markup") is None

    @pytest.mark.asyncio
    @patch("src.services.core.posting.settings")
    async def test_no_chat_id_at_all_returns_early(
        self, mock_settings, posting_service
    ):
        """Returns without sending when the chat is falsy."""
        mock_settings.ADMIN_TELEGRAM_CHAT_ID = None

        bot = AsyncMock()
        await posting_service.send_gdrive_auth_alert(None, bot=bot)

        bot.send_message.assert_not_called()
        posting_service.settings_service.get_settings.assert_not_called()

    @pytest.mark.asyncio
    @patch("src.services.core.posting.settings")
    async def test_send_failure_does_not_persist_flag(
        self, mock_settings, posting_service
    ):
        """If the Telegram send fails, the flag is NOT set — allow retry next tick."""
        mock_settings.ADMIN_TELEGRAM_CHAT_ID = -100123
        mock_settings.OAUTH_REDIRECT_BASE_URL = "https://example.com"
        posting_service.settings_service.get_settings.return_value = _chat_settings(
            alerted_at=None
        )

        bot = AsyncMock()
        bot.send_message.side_effect = RuntimeError("Network error")
        await posting_service.send_gdrive_auth_alert(-100123, bot=bot)

        posting_service.settings_service.set_gdrive_alerted_at.assert_not_called()
