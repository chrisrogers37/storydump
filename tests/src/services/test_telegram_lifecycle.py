"""Tests for TelegramLifecycleHandler — startup/shutdown notifications."""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.services.core.telegram_lifecycle import TelegramLifecycleHandler
from src.services.core.telegram_utils import format_last_post


@pytest.fixture
def mock_service():
    """Minimal TelegramService mock for lifecycle tests."""
    service = Mock()
    service.admin_chat_id = 12345
    service.bot = AsyncMock()
    # Default: lifecycle notifications enabled (admin chat row says so).
    # Individual tests override `send_lifecycle_notifications=False` to
    # exercise the skip path.
    service.settings_service.get_settings.return_value = Mock(
        send_lifecycle_notifications=True
    )
    return service


@pytest.fixture
def handler(mock_service):
    return TelegramLifecycleHandler(mock_service)


# ──────────────────────────────────────────────────────────────
# send_startup_notification — multi-instance view
# ──────────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
class TestSendStartupNotification:
    async def test_skips_when_notifications_disabled(self, handler):
        handler.service.settings_service.get_settings.return_value = Mock(
            send_lifecycle_notifications=False
        )
        await handler.send_startup_notification()
        handler.service.bot.send_message.assert_not_called()

    async def test_shows_instance_list(self, handler):

        mock_dash = Mock()
        mock_dash.get_user_instances.return_value = {
            "instances": [
                {
                    "display_name": "TL Enterprises",
                    "telegram_chat_id": -100123,
                    "media_count": 50,
                    "posts_per_day": 3,
                    "is_paused": False,
                    "last_post_at": None,
                    "chat_settings_id": "cs-1",
                },
            ],
        }
        mock_dash.__enter__ = Mock(return_value=mock_dash)
        mock_dash.__exit__ = Mock(return_value=False)

        with patch(
            "src.services.core.telegram_lifecycle.DashboardService",
            return_value=mock_dash,
        ):
            await handler.send_startup_notification()

        handler.service.bot.send_message.assert_called_once()
        text = handler.service.bot.send_message.call_args[1]["text"]
        assert "TL Enterprises" in text
        assert "3/day" in text
        assert "50 media" in text
        assert "Started" in text

    async def test_no_instances(self, handler):

        mock_dash = Mock()
        mock_dash.get_user_instances.return_value = {"instances": []}
        mock_dash.__enter__ = Mock(return_value=mock_dash)
        mock_dash.__exit__ = Mock(return_value=False)

        with patch(
            "src.services.core.telegram_lifecycle.DashboardService",
            return_value=mock_dash,
        ):
            await handler.send_startup_notification()

        text = handler.service.bot.send_message.call_args[1]["text"]
        assert "No instances configured" in text

    async def test_multiple_instances(self, handler):

        mock_dash = Mock()
        mock_dash.get_user_instances.return_value = {
            "instances": [
                {
                    "display_name": "Brand A",
                    "telegram_chat_id": -100,
                    "media_count": 10,
                    "posts_per_day": 2,
                    "is_paused": False,
                    "last_post_at": "2026-04-20T12:00:00+00:00",
                    "chat_settings_id": "cs-1",
                },
                {
                    "display_name": "Brand B",
                    "telegram_chat_id": -200,
                    "media_count": 5,
                    "posts_per_day": 1,
                    "is_paused": True,
                    "last_post_at": None,
                    "chat_settings_id": "cs-2",
                },
            ],
        }
        mock_dash.__enter__ = Mock(return_value=mock_dash)
        mock_dash.__exit__ = Mock(return_value=False)

        with patch(
            "src.services.core.telegram_lifecycle.DashboardService",
            return_value=mock_dash,
        ):
            await handler.send_startup_notification()

        text = handler.service.bot.send_message.call_args[1]["text"]
        assert "Brand A" in text
        assert "Brand B" in text
        assert "paused" in text


# ──────────────────────────────────────────────────────────────
# format_last_post
# ──────────────────────────────────────────────────────────────


@pytest.mark.unit
class TestFormatLastPost:
    def test_none_returns_never(self):
        assert format_last_post(None) == "never"

    def test_recent_post(self):
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        result = format_last_post(now.isoformat())
        assert result == "< 1h ago"

    def test_old_post_shows_days(self):
        from datetime import datetime, timedelta, timezone

        old = datetime.now(timezone.utc) - timedelta(days=3)
        result = format_last_post(old.isoformat())
        assert result == "3d ago"


@pytest.mark.unit
@pytest.mark.asyncio
class TestStartupNotificationSurvivesOneMalformedInstance:
    """#783 site 2: the per-instance formatting loop sat inside the method's
    single `try`, so one malformed row aborted the whole startup notification
    and the admin was told nothing at all rather than told about the rest.

    Lower stakes than the alert sweeps — one message at boot, and the loop
    formats rather than does I/O, so it fails only on bad data. But the failure
    mode is the same family: the loss is total and silent from the reader's
    side, because "no startup message" looks identical to "the bot did not
    start".
    """

    def _dash(self, instances):
        dash = Mock()
        dash.get_user_instances.return_value = {"instances": instances}
        dash.__enter__ = Mock(return_value=dash)
        dash.__exit__ = Mock(return_value=False)
        return dash

    def _good(self, name, chat_id):
        return {
            "display_name": name,
            "telegram_chat_id": chat_id,
            "media_count": 10,
            "posts_per_day": 2,
            "is_paused": False,
            "last_post_at": None,
            "chat_settings_id": "cs",
        }

    async def _run(self, handler, instances):
        with (
            patch(
                "src.services.core.telegram_lifecycle.DashboardService",
                return_value=self._dash(instances),
            ),
            patch("src.services.core.telegram_lifecycle.logger") as log,
        ):
            await handler.send_startup_notification()
        return log

    async def test_a_malformed_row_does_not_suppress_the_whole_notification(
        self, handler
    ):
        """THE REGRESSION. A row missing `media_count` raised KeyError inside
        the loop; before the fix the admin received no message at all."""
        bad = self._good("Broken Ltd", -100002)
        del bad["media_count"]
        instances = [self._good("First Co", -100001), bad, self._good("Third Co", -3)]

        await self._run(handler, instances)

        handler.service.bot.send_message.assert_called_once()
        text = handler.service.bot.send_message.call_args[1]["text"]
        assert "First Co" in text
        assert "Third Co" in text, (
            "the instance AFTER the malformed row is missing — the loop still "
            f"aborts early: {text}"
        )

    async def test_the_malformed_row_is_surfaced_not_silently_dropped(self, handler):
        """Matching #781: the skipped row stays visible. Dropping it quietly
        would trade a total loss for a partial one that nobody can see."""
        bad = self._good("Broken Ltd", -100002)
        del bad["media_count"]

        log = await self._run(handler, [self._good("First Co", -1), bad])

        lines = [str(c.args[0]) for c in log.warning.call_args_list]
        assert any("-100002" in ln for ln in lines), (
            f"the malformed instance is not named in any warning: {lines}"
        )

    async def test_all_good_rows_log_nothing(self, handler):
        """The control: a clean boot stays quiet, so the warning above means
        something."""
        log = await self._run(
            handler, [self._good("First Co", -1), self._good("Second Co", -2)]
        )

        handler.service.bot.send_message.assert_called_once()
        assert log.warning.call_args_list == []
