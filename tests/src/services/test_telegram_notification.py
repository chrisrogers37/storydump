"""Tests for TelegramNotificationService."""

import pytest
from unittest.mock import Mock, AsyncMock, patch
from uuid import uuid4

from src.config import defaults
from src.repositories.tenant_scope import SYSTEM_SCOPE
from src.exceptions.google_drive import GoogleDriveAuthError
from src.services.core.telegram_notification import (
    TelegramNotificationService,
    _extract_button_labels,
    _is_google_auth_error,
)


@pytest.fixture
def mock_telegram_service():
    """Mock parent TelegramService with required attributes."""
    service = Mock()
    service.bot = AsyncMock()
    service.bot_token = "123456:ABC-DEF"
    service.channel_id = -1001234567890
    service.media_repo = Mock()
    service.queue_repo = Mock()
    service.history_repo = Mock()
    service.settings_service = Mock()
    service.interaction_service = Mock()
    service.ig_account_service = Mock()
    service.ig_account_service.count_active_accounts.return_value = 1
    return service


@pytest.fixture
def notification_service(mock_telegram_service):
    """Create TelegramNotificationService with mocked parent."""
    return TelegramNotificationService(mock_telegram_service)


@pytest.mark.unit
class TestBuildCaption:
    """Tests for _build_caption routing."""

    def test_routes_to_enhanced_when_caption_style_enhanced(self, notification_service):
        """Test _build_caption routes to enhanced when CAPTION_STYLE is enhanced."""
        media = Mock(
            title="Test Image",
            caption=None,
            generated_caption=None,
            link_url=None,
            tags=[],
        )

        result = notification_service._build_caption(
            media, active_account=None, caption_style="enhanced"
        )

        # Enhanced caption includes "Account: Not set" for no account
        assert "Account: Not set" in result

    def test_routes_to_simple_when_caption_style_simple(self, notification_service):
        """Test _build_caption routes to simple when CAPTION_STYLE is simple."""
        media = Mock(
            title="Test Image",
            caption=None,
            generated_caption=None,
            link_url=None,
            tags=[],
            file_name="test.jpg",
            id="12345678-abcd",
        )

        result = notification_service._build_caption(
            media, verbose=True, active_account=None, caption_style="simple"
        )

        # Simple caption includes file info when verbose
        assert "File: test.jpg" in result

    def test_enhanced_caption_shows_active_account(self, notification_service):
        """Test enhanced caption shows active account display name."""
        media = Mock(
            title="Test Image",
            caption=None,
            generated_caption=None,
            link_url=None,
            tags=[],
        )
        account = Mock(display_name="Main Account")

        result = notification_service._build_caption(
            media, active_account=account, caption_style="enhanced"
        )

        assert "Account: Main Account" in result

    def test_enhanced_caption_shows_not_set_when_no_account(self, notification_service):
        """Test enhanced caption shows 'Not set' when no account."""
        media = Mock(
            title="Test Image",
            caption=None,
            generated_caption=None,
            link_url=None,
            tags=[],
        )

        result = notification_service._build_caption(
            media, active_account=None, caption_style="enhanced"
        )

        assert "Account: Not set" in result


@pytest.mark.unit
class TestBuildSimpleCaption:
    """Tests for _build_simple_caption formatting."""

    def test_includes_title(self, notification_service):
        """Test simple caption includes media title."""
        media = Mock(
            title="My Title",
            caption=None,
            generated_caption=None,
            link_url=None,
            tags=[],
            file_name="img.jpg",
            id="abcd1234",
        )

        result = notification_service._build_simple_caption(media)

        assert "My Title" in result

    def test_includes_force_sent_indicator(self, notification_service):
        """Test simple caption includes lightning bolt for force-sent."""
        media = Mock(
            title="Test",
            caption=None,
            generated_caption=None,
            link_url=None,
            tags=[],
            file_name="img.jpg",
            id="abcd1234",
        )

        result = notification_service._build_simple_caption(media, force_sent=True)

        assert "\u26a1" in result

    def test_verbose_shows_file_and_id(self, notification_service):
        """Test verbose=True includes file name and truncated ID."""
        media = Mock(
            title="Test",
            caption=None,
            generated_caption=None,
            link_url=None,
            tags=[],
            file_name="image.jpg",
            id="12345678-abcd-efgh",
        )

        result = notification_service._build_simple_caption(media, verbose=True)

        assert "File: image.jpg" in result
        assert "ID: 12345678" in result

    def test_verbose_off_hides_file_and_id(self, notification_service):
        """Test verbose=False omits file name and ID."""
        media = Mock(
            title="Test",
            caption=None,
            generated_caption=None,
            link_url=None,
            tags=[],
            file_name="image.jpg",
            id="12345678-abcd-efgh",
        )

        result = notification_service._build_simple_caption(media, verbose=False)

        assert "File:" not in result
        assert "ID:" not in result

    def test_includes_account_indicator(self, notification_service):
        """Test simple caption includes account when provided."""
        media = Mock(
            title="Test",
            caption=None,
            generated_caption=None,
            link_url=None,
            tags=[],
            file_name="img.jpg",
            id="abcd1234",
        )
        account = Mock(display_name="Brand Account")

        result = notification_service._build_simple_caption(
            media, active_account=account
        )

        assert "Brand Account" in result

    def test_includes_tags(self, notification_service):
        """Test simple caption includes tags as hashtags."""
        media = Mock(
            title="Test",
            caption=None,
            generated_caption=None,
            link_url=None,
            tags=["meme", "funny"],
            file_name="img.jpg",
            id="abcd1234",
        )

        result = notification_service._build_simple_caption(media, verbose=False)

        assert "#meme" in result
        assert "#funny" in result

    def test_includes_link_url(self, notification_service):
        """Test simple caption includes link URL."""
        media = Mock(
            title="Test",
            caption=None,
            generated_caption=None,
            link_url="https://example.com",
            tags=[],
            file_name="img.jpg",
            id="abcd1234",
        )

        result = notification_service._build_simple_caption(media, verbose=False)

        assert "https://example.com" in result


@pytest.mark.unit
class TestBuildEnhancedCaption:
    """Tests for _build_enhanced_caption formatting."""

    def test_verbose_on_shows_workflow_instructions(self, notification_service):
        """Test verbose=True includes workflow instructions."""
        media = Mock(
            title="Test",
            caption=None,
            generated_caption=None,
            link_url=None,
            tags=[],
        )

        result = notification_service._build_enhanced_caption(
            media, verbose=True, active_account=None
        )

        assert "Click & hold image" in result
        assert "Open Instagram" in result

    def test_verbose_off_hides_workflow_instructions(self, notification_service):
        """Test verbose=False omits workflow instructions."""
        media = Mock(
            title="Test",
            caption=None,
            generated_caption=None,
            link_url=None,
            tags=[],
        )

        result = notification_service._build_enhanced_caption(
            media, verbose=False, active_account=None
        )

        assert "Click & hold image" not in result
        assert "Open Instagram" not in result

    def test_verbose_on_shows_debug_info(self, notification_service):
        """Test verbose=True shows file name and ID in enhanced mode."""
        media = Mock(
            title="Test",
            caption=None,
            generated_caption=None,
            link_url=None,
            tags=[],
            file_name="image.jpg",
            id="12345678-abcd-efgh",
        )

        result = notification_service._build_enhanced_caption(
            media, verbose=True, active_account=None
        )

        assert "File: image.jpg" in result
        assert "ID: 12345678" in result

    def test_verbose_off_hides_debug_info(self, notification_service):
        """Test verbose=False omits file name and ID in enhanced mode."""
        media = Mock(
            title="Test",
            caption=None,
            generated_caption=None,
            link_url=None,
            tags=[],
            file_name="image.jpg",
            id="12345678-abcd-efgh",
        )

        result = notification_service._build_enhanced_caption(
            media, verbose=False, active_account=None
        )

        assert "File:" not in result
        assert "ID:" not in result

    def test_verbose_with_ig_login_account_hides_workflow_instructions(
        self, notification_service
    ):
        """Active account on instagram_login has Auto Post — manual steps hidden."""
        media = Mock(
            title="Test",
            caption=None,
            generated_caption=None,
            link_url=None,
            tags=[],
            file_name="image.jpg",
            id="12345678-abcd-efgh",
        )
        account = Mock(display_name="GT", auth_method="instagram_login")

        result = notification_service._build_enhanced_caption(
            media, verbose=True, active_account=account
        )

        # Debug info still rendered
        assert "File: image.jpg" in result
        assert "ID: 12345678" in result
        # Manual instructions suppressed for Auto Post chats
        assert "Click & hold image" not in result
        assert "Open Instagram" not in result
        assert "Post your story" not in result

    def test_verbose_with_fb_login_account_still_shows_workflow_instructions(
        self, notification_service
    ):
        """Legacy fb_login accounts have no Auto Post — manual steps still shown."""
        media = Mock(
            title="Test",
            caption=None,
            generated_caption=None,
            link_url=None,
            tags=[],
            file_name="image.jpg",
            id="12345678-abcd-efgh",
        )
        account = Mock(display_name="Legacy", auth_method="fb_login")

        result = notification_service._build_enhanced_caption(
            media, verbose=True, active_account=account
        )

        assert "Click & hold image" in result
        assert "Open Instagram" in result

    def test_verbose_off_still_shows_account(self, notification_service):
        """Test verbose=False still shows the account indicator."""
        media = Mock(
            title="Test",
            caption=None,
            generated_caption=None,
            link_url=None,
            tags=[],
        )
        account = Mock(display_name="My Brand")

        result = notification_service._build_enhanced_caption(
            media, verbose=False, active_account=account
        )

        assert "My Brand" in result

    def test_force_sent_shows_lightning(self, notification_service):
        """Test force_sent=True shows lightning bolt."""
        media = Mock(
            title="Test",
            caption=None,
            generated_caption=None,
            link_url=None,
            tags=[],
        )

        result = notification_service._build_enhanced_caption(
            media, force_sent=True, active_account=None
        )

        assert "\u26a1" in result

    def test_includes_caption_text(self, notification_service):
        """Test enhanced caption includes media caption."""
        media = Mock(
            title="Test",
            caption="This is the caption text",
            link_url=None,
            tags=[],
        )

        result = notification_service._build_enhanced_caption(
            media, active_account=None
        )

        assert "This is the caption text" in result

    def test_includes_tags(self, notification_service):
        """Test enhanced caption includes hashtags."""
        media = Mock(
            title="Test",
            caption=None,
            generated_caption=None,
            link_url=None,
            tags=["product", "sale"],
        )

        result = notification_service._build_enhanced_caption(
            media, verbose=False, active_account=None
        )

        assert "#product" in result
        assert "#sale" in result


@pytest.mark.unit
class TestGetHeaderEmoji:
    """Tests for _get_header_emoji."""

    def test_no_tags_returns_camera(self, notification_service):
        """Test empty/None tags returns camera emoji."""
        assert notification_service._get_header_emoji(None) == "\U0001f4f8"
        assert notification_service._get_header_emoji([]) == "\U0001f4f8"

    def test_meme_tag_returns_laughing(self, notification_service):
        """Test meme-related tags return laughing emoji."""
        assert notification_service._get_header_emoji(["meme"]) == "\U0001f602"
        assert notification_service._get_header_emoji(["funny"]) == "\U0001f602"
        assert notification_service._get_header_emoji(["humor"]) == "\U0001f602"

    def test_product_tag_returns_shopping(self, notification_service):
        """Test product-related tags return shopping emoji."""
        assert notification_service._get_header_emoji(["product"]) == "\U0001f6cd\ufe0f"
        assert notification_service._get_header_emoji(["shop"]) == "\U0001f6cd\ufe0f"
        assert notification_service._get_header_emoji(["sale"]) == "\U0001f6cd\ufe0f"

    def test_quote_tag_returns_sparkle(self, notification_service):
        """Test quote-related tags return sparkle emoji."""
        assert notification_service._get_header_emoji(["quote"]) == "\u2728"
        assert notification_service._get_header_emoji(["inspiration"]) == "\u2728"

    def test_announcement_tag_returns_megaphone(self, notification_service):
        """Test announcement-related tags return megaphone emoji."""
        assert notification_service._get_header_emoji(["news"]) == "\U0001f4e2"
        assert notification_service._get_header_emoji(["announcement"]) == "\U0001f4e2"

    def test_question_tag_returns_speech_bubble(self, notification_service):
        """Test question-related tags return speech bubble emoji."""
        assert notification_service._get_header_emoji(["poll"]) == "\U0001f4ac"
        assert notification_service._get_header_emoji(["question"]) == "\U0001f4ac"

    def test_unknown_tag_returns_camera(self, notification_service):
        """Test unknown tags return default camera emoji."""
        assert notification_service._get_header_emoji(["random"]) == "\U0001f4f8"
        assert notification_service._get_header_emoji(["unknown"]) == "\U0001f4f8"

    def test_case_insensitive(self, notification_service):
        """Test tag matching is case-insensitive."""
        assert notification_service._get_header_emoji(["MEME"]) == "\U0001f602"
        assert notification_service._get_header_emoji(["Product"]) == "\U0001f6cd\ufe0f"


@pytest.mark.unit
class TestBuildKeyboard:
    """Tests for build_queue_action_keyboard (via telegram_utils)."""

    def test_includes_autopost_when_api_enabled(self):
        """Test keyboard includes Auto Post button when Instagram API is on."""
        from src.services.core.telegram_utils import build_queue_action_keyboard

        queue_id = str(uuid4())
        active_account = Mock(display_name="Test Account")

        result = build_queue_action_keyboard(
            queue_id, enable_instagram_api=True, active_account=active_account
        )

        buttons = [b.text for row in result.inline_keyboard for b in row]
        assert any("Auto Post" in b for b in buttons)

    def test_excludes_autopost_when_api_disabled(self):
        """Test keyboard excludes Auto Post button when Instagram API is off."""
        from src.services.core.telegram_utils import build_queue_action_keyboard

        queue_id = str(uuid4())
        active_account = Mock(display_name="Test Account")

        result = build_queue_action_keyboard(
            queue_id, enable_instagram_api=False, active_account=active_account
        )

        buttons = [b.text for row in result.inline_keyboard for b in row]
        assert not any("Auto Post" in b for b in buttons)

    def test_includes_posted_skip_reject_buttons(self):
        """Test keyboard always includes Posted, Skip, and Reject buttons."""
        from src.services.core.telegram_utils import build_queue_action_keyboard

        queue_id = str(uuid4())

        result = build_queue_action_keyboard(
            queue_id, enable_instagram_api=False, active_account=None
        )

        buttons = [b.text for row in result.inline_keyboard for b in row]
        assert any("Posted" in b for b in buttons)
        assert any("Skip" in b for b in buttons)
        assert any("Reject" in b for b in buttons)

    def test_shows_account_display_name(self):
        """Test account selector button shows display name."""
        from src.services.core.telegram_utils import build_queue_action_keyboard

        queue_id = str(uuid4())
        active_account = Mock(display_name="My Brand")

        result = build_queue_action_keyboard(
            queue_id, enable_instagram_api=False, active_account=active_account
        )

        buttons = [b.text for row in result.inline_keyboard for b in row]
        assert any("My Brand" in b for b in buttons)

    def test_shows_no_account_when_none(self):
        """Test account selector shows 'No Account' when none configured."""
        from src.services.core.telegram_utils import build_queue_action_keyboard

        queue_id = str(uuid4())

        result = build_queue_action_keyboard(
            queue_id, enable_instagram_api=False, active_account=None
        )

        buttons = [b.text for row in result.inline_keyboard for b in row]
        assert any("No Account" in b for b in buttons)

    def test_includes_open_instagram_link(self):
        """Test keyboard includes Open Instagram button with URL."""
        from src.services.core.telegram_utils import build_queue_action_keyboard

        queue_id = str(uuid4())

        result = build_queue_action_keyboard(
            queue_id, enable_instagram_api=False, active_account=None
        )

        for row in result.inline_keyboard:
            for button in row:
                if "Instagram" in button.text:
                    assert button.url == defaults.DEFAULT_INSTAGRAM_DEEPLINK_URL
                    return

        pytest.fail("Open Instagram button not found")


@pytest.mark.unit
class TestExtractButtonLabels:
    """Tests for _extract_button_labels helper."""

    def test_extracts_labels(self):
        """Test extracting button labels from markup."""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Button 1", callback_data="a"),
                    InlineKeyboardButton("Button 2", callback_data="b"),
                ],
                [InlineKeyboardButton("Button 3", callback_data="c")],
            ]
        )

        labels = _extract_button_labels(markup)

        assert labels == ["Button 1", "Button 2", "Button 3"]

    def test_returns_empty_for_none(self):
        """Test returns empty list for None markup."""
        assert _extract_button_labels(None) == []

    def test_returns_empty_for_no_keyboard(self):
        """Test returns empty list for object without inline_keyboard."""
        assert _extract_button_labels(Mock(spec=[])) == []


@pytest.mark.unit
@pytest.mark.asyncio
class TestSendNotification:
    """Tests for send_notification."""

    async def test_returns_false_when_queue_item_not_found(
        self, notification_service, mock_telegram_service
    ):
        """Test send_notification returns False when queue item doesn't exist."""
        mock_telegram_service.queue_repo.get_by_id.return_value = None

        result = await notification_service.send_notification("nonexistent-id")

        assert result is False

    async def test_returns_false_when_media_item_not_found(
        self, notification_service, mock_telegram_service
    ):
        """Test send_notification returns False when media item doesn't exist."""
        queue_item = Mock(media_item_id=uuid4(), telegram_message_id=None)
        mock_telegram_service.queue_repo.get_by_id.return_value = queue_item
        mock_telegram_service.media_repo.get_by_id.return_value = None

        result = await notification_service.send_notification("some-id")

        assert result is False

    async def test_initializes_bot_if_none(
        self, notification_service, mock_telegram_service
    ):
        """Test bot is initialized if not already set."""
        mock_telegram_service.bot = None
        mock_telegram_service.queue_repo.get_by_id.return_value = None

        with patch("telegram.Bot") as mock_bot_class:
            mock_bot_class.return_value = Mock()
            await notification_service.send_notification("some-id")

        # Bot should have been created (even though queue item not found)
        mock_bot_class.assert_called_once_with(token=mock_telegram_service.bot_token)

    async def test_sends_photo_on_success(
        self, notification_service, mock_telegram_service
    ):
        """Test successful notification sends photo to channel."""
        queue_item_id = str(uuid4())
        queue_item = Mock(media_item_id=uuid4(), telegram_message_id=None)
        media_item = Mock(
            file_name="test.jpg",
            title="Test",
            caption=None,
            generated_caption=None,
            link_url=None,
            tags=[],
            source_identifier="test.jpg",
        )

        mock_telegram_service.queue_repo.get_by_id.return_value = queue_item
        mock_telegram_service.media_repo.get_by_id.return_value = media_item
        mock_telegram_service.settings_service.get_settings.return_value = Mock(
            enable_instagram_api=False,
            show_verbose_notifications=True,
        )
        mock_telegram_service._is_verbose.return_value = True
        mock_telegram_service.ig_account_service.get_active_account.return_value = None

        # Mock the send_photo to return a message with message_id
        mock_message = Mock(message_id=12345)
        mock_telegram_service.bot.send_photo = AsyncMock(return_value=mock_message)

        # Mock MediaSourceFactory
        mock_provider = Mock()
        mock_provider.download_file.return_value = b"fake-image-bytes"

        with patch(
            "src.services.media_sources.factory.MediaSourceFactory"
        ) as mock_factory:
            mock_factory.get_provider_for_media_item.return_value = mock_provider

            result = await notification_service.send_notification(queue_item_id)

        assert result is True
        mock_telegram_service.bot.send_photo.assert_called_once()

    async def test_returns_false_on_send_error(
        self, notification_service, mock_telegram_service
    ):
        """Test returns False when sending fails."""
        queue_item = Mock(media_item_id=uuid4(), telegram_message_id=None)
        media_item = Mock(
            file_name="test.jpg",
            title="Test",
            caption=None,
            generated_caption=None,
            link_url=None,
            tags=[],
            source_identifier="test.jpg",
        )

        mock_telegram_service.queue_repo.get_by_id.return_value = queue_item
        mock_telegram_service.media_repo.get_by_id.return_value = media_item
        mock_telegram_service.settings_service.get_settings.return_value = Mock(
            enable_instagram_api=False,
            show_verbose_notifications=True,
        )
        mock_telegram_service._is_verbose.return_value = True
        mock_telegram_service.ig_account_service.get_active_account.return_value = None

        # Mock provider to raise an error
        mock_provider = Mock()
        mock_provider.download_file.side_effect = Exception("Download failed")

        with patch(
            "src.services.media_sources.factory.MediaSourceFactory"
        ) as mock_factory:
            mock_factory.get_provider_for_media_item.return_value = mock_provider

            result = await notification_service.send_notification("some-id")

        assert result is False

    async def test_google_drive_auth_error_propagates(
        self, notification_service, mock_telegram_service
    ):
        """GoogleDriveAuthError from provider should propagate, not be swallowed."""
        queue_item = Mock(media_item_id=uuid4(), telegram_message_id=None)
        media_item = Mock(
            file_name="test.jpg",
            title="Test",
            caption=None,
            generated_caption=None,
            link_url=None,
            tags=[],
            source_identifier="test.jpg",
        )

        mock_telegram_service.queue_repo.get_by_id.return_value = queue_item
        mock_telegram_service.media_repo.get_by_id.return_value = media_item
        mock_telegram_service.settings_service.get_settings.return_value = Mock(
            enable_instagram_api=False,
            show_verbose_notifications=True,
        )
        mock_telegram_service._is_verbose.return_value = True
        mock_telegram_service.ig_account_service.get_active_account.return_value = None

        mock_provider = Mock()
        mock_provider.download_file.side_effect = GoogleDriveAuthError("Token expired")

        with patch(
            "src.services.media_sources.factory.MediaSourceFactory"
        ) as mock_factory:
            mock_factory.get_provider_for_media_item.return_value = mock_provider

            with pytest.raises(GoogleDriveAuthError, match="Token expired"):
                await notification_service.send_notification("some-id")

    async def test_refresh_error_converted_to_google_drive_auth_error(
        self, notification_service, mock_telegram_service
    ):
        """google.auth RefreshError in __cause__ chain should convert to GoogleDriveAuthError."""
        queue_item = Mock(media_item_id=uuid4(), telegram_message_id=None)
        media_item = Mock(
            file_name="test.jpg",
            title="Test",
            caption=None,
            generated_caption=None,
            link_url=None,
            tags=[],
            source_identifier="test.jpg",
        )

        mock_telegram_service.queue_repo.get_by_id.return_value = queue_item
        mock_telegram_service.media_repo.get_by_id.return_value = media_item
        mock_telegram_service.settings_service.get_settings.return_value = Mock(
            enable_instagram_api=False,
            show_verbose_notifications=True,
        )
        mock_telegram_service._is_verbose.return_value = True
        mock_telegram_service.ig_account_service.get_active_account.return_value = None

        # Simulate a google.auth RefreshError wrapped in a generic exception
        # Create a fake RefreshError-like class
        fake_refresh_error = type(
            "RefreshError", (Exception,), {"__module__": "google.auth.exceptions"}
        )("token revoked")
        wrapper = RuntimeError("Download failed")
        wrapper.__cause__ = fake_refresh_error

        mock_provider = Mock()
        mock_provider.download_file.side_effect = wrapper

        with patch(
            "src.services.media_sources.factory.MediaSourceFactory"
        ) as mock_factory:
            mock_factory.get_provider_for_media_item.return_value = mock_provider

            with pytest.raises(GoogleDriveAuthError, match="expired or revoked"):
                await notification_service.send_notification("some-id")

    async def test_non_auth_error_still_returns_false(
        self, notification_service, mock_telegram_service
    ):
        """Non-auth exceptions should still be caught and return False."""
        queue_item = Mock(media_item_id=uuid4(), telegram_message_id=None)
        media_item = Mock(
            file_name="test.jpg",
            title="Test",
            caption=None,
            generated_caption=None,
            link_url=None,
            tags=[],
            source_identifier="test.jpg",
        )

        mock_telegram_service.queue_repo.get_by_id.return_value = queue_item
        mock_telegram_service.media_repo.get_by_id.return_value = media_item
        mock_telegram_service.settings_service.get_settings.return_value = Mock(
            enable_instagram_api=False,
            show_verbose_notifications=True,
        )
        mock_telegram_service._is_verbose.return_value = True
        mock_telegram_service.ig_account_service.get_active_account.return_value = None

        mock_provider = Mock()
        mock_provider.download_file.side_effect = ConnectionError("Network down")

        with patch(
            "src.services.media_sources.factory.MediaSourceFactory"
        ) as mock_factory:
            mock_factory.get_provider_for_media_item.return_value = mock_provider

            result = await notification_service.send_notification("some-id")

        assert result is False

    async def test_no_op_when_card_already_delivered(
        self, notification_service, mock_telegram_service
    ):
        """A queue item that already carries a telegram_message_id must not be
        sent again — the retry path re-enters here after a timed-out-but-
        delivered send, and a second send posts a duplicate approval card."""
        queue_item = Mock(media_item_id=uuid4(), telegram_message_id=55555)
        media_item = Mock(
            file_name="test.jpg",
            title="Test",
            caption=None,
            generated_caption=None,
            link_url=None,
            tags=[],
            source_identifier="test.jpg",
        )
        mock_telegram_service.queue_repo.get_by_id.return_value = queue_item
        mock_telegram_service.media_repo.get_by_id.return_value = media_item
        mock_telegram_service._is_verbose.return_value = True
        mock_telegram_service.ig_account_service.get_active_account.return_value = None
        mock_telegram_service.bot.send_photo = AsyncMock(
            return_value=Mock(message_id=99999)
        )

        mock_provider = Mock()
        mock_provider.download_file.return_value = b"fake-image-bytes"

        with patch(
            "src.services.media_sources.factory.MediaSourceFactory"
        ) as mock_factory:
            mock_factory.get_provider_for_media_item.return_value = mock_provider

            result = await notification_service.send_notification("some-id")

        assert result is True
        mock_telegram_service.bot.send_photo.assert_not_called()
        mock_telegram_service.queue_repo.set_telegram_message.assert_not_called()

    async def test_timed_out_send_raises_ambiguous_delivery(
        self, notification_service, mock_telegram_service
    ):
        """TimedOut from send_photo is ambiguous — the card may have been
        delivered. It must surface as AmbiguousDeliveryError so the caller
        can decide not to resend, instead of being swallowed into a
        retryable False."""
        from telegram.error import TimedOut

        from src.exceptions.telegram import AmbiguousDeliveryError

        queue_item = Mock(media_item_id=uuid4(), telegram_message_id=None)
        media_item = Mock(
            file_name="test.jpg",
            title="Test",
            caption=None,
            generated_caption=None,
            link_url=None,
            tags=[],
            source_identifier="test.jpg",
        )

        mock_telegram_service.queue_repo.get_by_id.return_value = queue_item
        mock_telegram_service.media_repo.get_by_id.return_value = media_item
        mock_telegram_service._is_verbose.return_value = True
        mock_telegram_service.ig_account_service.get_active_account.return_value = None
        mock_telegram_service.bot.send_photo = AsyncMock(
            side_effect=TimedOut("Pool timeout")
        )

        mock_provider = Mock()
        mock_provider.download_file.return_value = b"fake-image-bytes"

        with patch(
            "src.services.media_sources.factory.MediaSourceFactory"
        ) as mock_factory:
            mock_factory.get_provider_for_media_item.return_value = mock_provider

            with pytest.raises(AmbiguousDeliveryError):
                await notification_service.send_notification("some-id")

    async def test_bookkeeping_failure_after_delivery_returns_true(
        self, notification_service, mock_telegram_service
    ):
        """Once send_photo has returned, the card IS in the chat. A failure
        stamping the message id (or logging) must not report the send as
        failed — that would trigger a retry and a duplicate card."""
        queue_item = Mock(media_item_id=uuid4(), telegram_message_id=None)
        media_item = Mock(
            file_name="test.jpg",
            title="Test",
            caption=None,
            generated_caption=None,
            link_url=None,
            tags=[],
            source_identifier="test.jpg",
        )

        mock_telegram_service.queue_repo.get_by_id.return_value = queue_item
        mock_telegram_service.media_repo.get_by_id.return_value = media_item
        mock_telegram_service._is_verbose.return_value = True
        mock_telegram_service.ig_account_service.get_active_account.return_value = None
        mock_telegram_service.bot.send_photo = AsyncMock(
            return_value=Mock(message_id=12345)
        )
        mock_telegram_service.queue_repo.set_telegram_message.side_effect = (
            RuntimeError("DB connection lost")
        )

        mock_provider = Mock()
        mock_provider.download_file.return_value = b"fake-image-bytes"

        with patch(
            "src.services.media_sources.factory.MediaSourceFactory"
        ) as mock_factory:
            mock_factory.get_provider_for_media_item.return_value = mock_provider

            result = await notification_service.send_notification("some-id")

        assert result is True


@pytest.mark.unit
class TestTenantRouting:
    """#541 — every per-tenant decision in send_notification must use the
    queue item's own tenant chat, never the deployment-wide
    TELEGRAM_CHANNEL_ID env chat."""

    TENANT_CHAT_ID = -1009876543210
    GLOBAL_CHAT_ID = -1001234567890

    def _make_media_item(self):
        return Mock(
            file_name="tenant.jpg",
            title="Tenant",
            caption=None,
            generated_caption=None,
            link_url=None,
            tags=[],
            source_identifier="tenant.jpg",
        )

    def _make_chat_settings(self, telegram_chat_id):
        return Mock(
            telegram_chat_id=telegram_chat_id,
            enable_instagram_api=False,
            caption_style=None,
        )

    def _wire(self, mock_telegram_service, queue_item):
        mock_telegram_service.queue_repo.get_by_id.return_value = queue_item
        mock_telegram_service.media_repo.get_by_id.return_value = (
            self._make_media_item()
        )
        mock_telegram_service._is_verbose.return_value = True
        mock_telegram_service.ig_account_service.get_active_account.return_value = None
        mock_telegram_service.bot.send_photo = AsyncMock(
            return_value=Mock(message_id=12345)
        )

    async def _send(self, notification_service, queue_item_id, force_sent=False):
        """Run send_notification with the media provider patched, returning
        (result, mock_factory) for provider-call assertions."""
        mock_provider = Mock()
        mock_provider.download_file.return_value = b"fake-image-bytes"
        with patch(
            "src.services.media_sources.factory.MediaSourceFactory"
        ) as mock_factory:
            mock_factory.get_provider_for_media_item.return_value = mock_provider
            result = await notification_service.send_notification(
                queue_item_id, force_sent=force_sent
            )
        return result, mock_factory

    async def test_routes_all_sends_to_tenant_chat(
        self, notification_service, mock_telegram_service
    ):
        """A queue item owned by tenant B is sent to B's chat: photo
        destination, settings, active account, media credentials, stored
        message chat, and interaction log all use B's chat id."""
        tenant_cs_id = uuid4()
        queue_item_id = str(uuid4())
        queue_item = Mock(
            id=queue_item_id,
            media_item_id=uuid4(),
            telegram_message_id=None,
            chat_settings_id=tenant_cs_id,
        )
        tenant_settings = self._make_chat_settings(self.TENANT_CHAT_ID)
        mock_telegram_service.settings_service.get_settings_by_id.return_value = (
            tenant_settings
        )
        self._wire(mock_telegram_service, queue_item)

        # force_sent=True also covers the /next → force_send_next path,
        # which reaches the same send.
        result, mock_factory = await self._send(
            notification_service, queue_item_id, force_sent=True
        )

        assert result is True
        # Tenant resolved by the queue item's chat_settings_id
        mock_telegram_service.settings_service.get_settings_by_id.assert_called_once_with(
            str(tenant_cs_id), chat_settings_id=SYSTEM_SCOPE
        )
        # The notification lands in the tenant's chat, not the env chat
        send_kwargs = mock_telegram_service.bot.send_photo.call_args.kwargs
        assert send_kwargs["chat_id"] == self.TENANT_CHAT_ID
        # Active IG account looked up for the tenant
        mock_telegram_service.ig_account_service.get_active_account.assert_called_once_with(
            self.TENANT_CHAT_ID
        )
        # Media bytes fetched with the tenant's source credentials
        factory_kwargs = mock_factory.get_provider_for_media_item.call_args.kwargs
        assert factory_kwargs["telegram_chat_id"] == self.TENANT_CHAT_ID
        # Queue row records the tenant chat id
        mock_telegram_service.queue_repo.set_telegram_message.assert_called_once_with(
            queue_item_id, 12345, self.TENANT_CHAT_ID, str(tenant_cs_id)
        )
        # Interaction log attributed to the tenant chat
        log_kwargs = (
            mock_telegram_service.interaction_service.log_bot_response.call_args.kwargs
        )
        assert log_kwargs["telegram_chat_id"] == self.TENANT_CHAT_ID
        # Verbose preference read for the tenant
        verbose_args = mock_telegram_service._is_verbose.call_args
        assert verbose_args.args[0] == self.TENANT_CHAT_ID
        # The env-chat settings lookup is never consulted
        mock_telegram_service.settings_service.get_settings.assert_not_called()

    async def test_null_chat_settings_id_falls_back_to_global_channel(
        self, notification_service, mock_telegram_service
    ):
        """Legacy rows (chat_settings_id NULL) keep today's behavior: the
        deployment-wide TELEGRAM_CHANNEL_ID chat."""
        queue_item = Mock(
            id=str(uuid4()),
            media_item_id=uuid4(),
            telegram_message_id=None,
            chat_settings_id=None,
        )
        mock_telegram_service.settings_service.get_settings.return_value = (
            self._make_chat_settings(self.GLOBAL_CHAT_ID)
        )
        self._wire(mock_telegram_service, queue_item)

        result, _ = await self._send(notification_service, str(queue_item.id))

        assert result is True
        mock_telegram_service.settings_service.get_settings.assert_called_once_with(
            self.GLOBAL_CHAT_ID
        )
        mock_telegram_service.settings_service.get_settings_by_id.assert_not_called()
        send_kwargs = mock_telegram_service.bot.send_photo.call_args.kwargs
        assert send_kwargs["chat_id"] == self.GLOBAL_CHAT_ID

    async def test_dangling_chat_settings_id_falls_back_to_global_channel(
        self, notification_service, mock_telegram_service
    ):
        """A chat_settings_id pointing at a deleted row falls back to the
        env chat instead of crashing or silently dropping the send."""
        queue_item = Mock(
            id=str(uuid4()),
            media_item_id=uuid4(),
            telegram_message_id=None,
            chat_settings_id=uuid4(),
        )
        mock_telegram_service.settings_service.get_settings_by_id.return_value = None
        mock_telegram_service.settings_service.get_settings.return_value = (
            self._make_chat_settings(self.GLOBAL_CHAT_ID)
        )
        self._wire(mock_telegram_service, queue_item)

        result, _ = await self._send(notification_service, str(queue_item.id))

        assert result is True
        mock_telegram_service.settings_service.get_settings_by_id.assert_called_once()
        send_kwargs = mock_telegram_service.bot.send_photo.call_args.kwargs
        assert send_kwargs["chat_id"] == self.GLOBAL_CHAT_ID


@pytest.mark.unit
class TestIsGoogleAuthError:
    """Tests for _is_google_auth_error helper."""

    def test_direct_refresh_error(self):
        """Detects a direct RefreshError-like exception."""
        FakeRefreshError = type(
            "RefreshError", (Exception,), {"__module__": "google.auth.exceptions"}
        )
        assert _is_google_auth_error(FakeRefreshError("token revoked")) is True

    def test_refresh_error_in_cause_chain(self):
        """Detects RefreshError nested in __cause__ chain."""
        FakeRefreshError = type(
            "RefreshError", (Exception,), {"__module__": "google.auth.exceptions"}
        )
        wrapper = RuntimeError("outer error")
        wrapper.__cause__ = FakeRefreshError("inner")
        assert _is_google_auth_error(wrapper) is True

    def test_unrelated_error_returns_false(self):
        """Returns False for unrelated exceptions."""
        assert _is_google_auth_error(ValueError("something")) is False

    def test_non_google_refresh_error_returns_false(self):
        """Returns False for RefreshError from non-google module."""
        FakeRefreshError = type(
            "RefreshError", (Exception,), {"__module__": "some.other.module"}
        )
        assert _is_google_auth_error(FakeRefreshError("nope")) is False


@pytest.mark.unit
class TestTenantCredentialResolutionFailsClosed:
    """A failure to RESOLVE a tenant's credentials surfaces as that tenant's error.

    Distinct from the ``download_file`` tests above, which inject after
    resolution already succeeded and a provider object exists. These inject at
    ``get_provider_for_media_item`` — the tenant-credential path itself, and the
    call whose broad ``except`` was removed so a named tenant can no longer fall
    through to the deployment-wide service account.

    The two are not interchangeable. Both raise sites inside
    ``get_provider_for_chat`` (no stored credentials; no configured root folder)
    fire *before* any provider is returned, so a test that can only inject on the
    provider cannot reach either of them. Moving the resolution call out of the
    guarded block is a refactor the ``download_file`` tests stay green through.
    """

    TENANT = -100777

    def _queue_and_media(self, mock_telegram_service):
        """Wire the repos so send_notification reaches credential resolution."""
        queue_item = Mock(media_item_id=uuid4(), telegram_message_id=None)
        media_item = Mock(
            file_name="test.jpg",
            title="Test",
            caption=None,
            generated_caption=None,
            link_url=None,
            tags=[],
            source_identifier="test.jpg",
        )
        mock_telegram_service.queue_repo.get_by_id.return_value = queue_item
        mock_telegram_service.media_repo.get_by_id.return_value = media_item
        # Bind the tenant explicitly: the assertions below are about WHICH
        # tenant the failure is attributed to, not merely that one failed.
        tenant_settings = Mock(
            telegram_chat_id=self.TENANT,
            enable_instagram_api=False,
            show_verbose_notifications=True,
        )
        mock_telegram_service.settings_service.get_settings_by_id.return_value = (
            tenant_settings
        )
        mock_telegram_service.settings_service.get_settings.return_value = (
            tenant_settings
        )
        mock_telegram_service._is_verbose.return_value = True
        mock_telegram_service.ig_account_service.get_active_account.return_value = None

    @pytest.mark.asyncio
    async def test_a_resolution_auth_failure_is_raised_not_returned_as_false(
        self, notification_service, mock_telegram_service
    ):
        """The tenant's own auth error propagates; it does not become ``False``.

        ``False`` is the generic send failure. Narrowing an auth error into it
        loses the one fact that distinguishes "this tenant must reconnect Drive"
        from "Telegram was briefly unhappy", and it is the shape #627 shipped.
        """
        self._queue_and_media(mock_telegram_service)

        with patch(
            "src.services.media_sources.factory.MediaSourceFactory"
        ) as mock_factory:
            mock_factory.get_provider_for_media_item.side_effect = GoogleDriveAuthError(
                "No Google Drive OAuth credentials found for this chat."
            )

            with pytest.raises(GoogleDriveAuthError, match="No Google Drive OAuth"):
                await notification_service.send_notification("some-id")

        # Anti-vacuity: a fail-closed assertion is worthless if the path was
        # never walked. Prove resolution was actually attempted, and for THIS
        # tenant — otherwise this test passes just as well against code that
        # returns before ever reaching the credential lookup.
        mock_factory.get_provider_for_media_item.assert_called_once()
        assert (
            mock_factory.get_provider_for_media_item.call_args.kwargs[
                "telegram_chat_id"
            ]
            == self.TENANT
        )
        # And it failed AT resolution rather than somewhere downstream.
        mock_telegram_service.bot.send_photo.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_non_auth_resolution_failure_still_returns_false(
        self, notification_service, mock_telegram_service
    ):
        """Control: the guard is specific to auth, not "everything propagates".

        Without this, the test above passes against a caller that has no
        exception handling at all — every failure would escape and the auth
        assertion would be measuring nothing about auth.
        """
        self._queue_and_media(mock_telegram_service)

        with patch(
            "src.services.media_sources.factory.MediaSourceFactory"
        ) as mock_factory:
            mock_factory.get_provider_for_media_item.side_effect = ValueError(
                "malformed source config"
            )

            result = await notification_service.send_notification("some-id")

        assert result is False
        mock_factory.get_provider_for_media_item.assert_called_once()
