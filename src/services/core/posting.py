"""Posting service - Google Drive auth alerts and posting utilities."""

from datetime import datetime, timezone

from telegram import Bot

from src.services.base_service import BaseService
from src.services.core.settings_service import SettingsService
from src.services.core.telegram_utils import GDRIVE_RECONNECT_GUIDANCE
from src.utils.logger import logger


class PostingService(BaseService):
    """Posting utilities and Google Drive auth alert management.

    The main scheduling and sending logic has moved to SchedulerService
    (JIT model). PostingService retains the Google Drive auth alert
    (state-transition notification) used by the scheduler loop when a
    GoogleDriveAuthError is encountered.
    """

    def __init__(self):
        super().__init__()
        self.settings_service = SettingsService()

    async def send_gdrive_auth_alert(self, telegram_chat_id: int, *, bot: Bot) -> None:
        """Send a Google Drive reconnect alert to Telegram.

        Gated on chat_settings.gdrive_alerted_at: fires once per disconnect
        event and stays silent until the OAuth reconnect callback clears
        the flag. State lives in Postgres so it survives worker restarts
        and is correctly scoped per chat.

        The caller supplies the bot — in the worker that is the
        Application's rate-limited ExtBot, so the alert shares the same
        outbound pacing as every other send instead of bypassing it via a
        raw Bot.

        The chat is REQUIRED (#867). This previously fell back to
        ADMIN_TELEGRAM_CHAT_ID on an absent id, with nothing in the docstring
        saying so — a tenant's reconnect alert would have been delivered to
        the admin chat instead. The one caller (the scheduler tick) has always
        passed a real tenant id, so nothing observable changes; the branch is
        gone so it cannot start mattering.
        """
        chat_id = telegram_chat_id
        if not chat_id:
            return

        chat_settings = self.settings_service.get_settings(chat_id)
        if chat_settings is None:
            logger.debug(
                f"Skipping Google Drive auth alert: no chat_settings for {chat_id}"
            )
            return
        if chat_settings.gdrive_alerted_at is not None:
            logger.debug(
                f"Skipping Google Drive auth alert for {chat_id}: "
                "already alerted, awaiting reconnect"
            )
            return

        try:
            text = (
                "⚠️ *Google Drive Disconnected*\n\n"
                "Your Google Drive token has expired or been revoked. "
                "Scheduled posts are paused until you reconnect.\n\n"
                f"{GDRIVE_RECONNECT_GUIDANCE}"
            )

            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="Markdown",
            )
            logger.info(f"Sent Google Drive auth alert to chat {chat_id}")

        except Exception as e:  # noqa: BLE001 — best-effort alert
            logger.error(f"Failed to send Google Drive auth alert: {e}")
            return

        self.settings_service.set_gdrive_alerted_at(chat_id, datetime.now(timezone.utc))
