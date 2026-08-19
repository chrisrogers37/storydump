"""Notification sending and caption building for Telegram."""

from typing import Optional

from telegram.error import ChatMigrated, TimedOut

from src.config import defaults
from src.exceptions.google_drive import GoogleDriveAuthError
from src.exceptions.telegram import AmbiguousDeliveryError, ChatMigratedError
from src.exceptions.tenancy import TenantResolutionError
from src.services.core.telegram_utils import escape_markdown as _escape_md
from src.utils.logger import logger
from src.repositories.tenant_scope import SYSTEM_SCOPE


def _is_google_auth_error(exc: Exception) -> bool:
    """Check if an exception is a Google auth/refresh error.

    Walks the ``__cause__`` chain to detect
    ``google.auth.exceptions.RefreshError`` without requiring the
    google-auth package as a hard import (it may not be installed in
    all environments).
    """
    current = exc
    while current is not None:
        type_name = type(current).__qualname__
        module = type(current).__module__ or ""
        if "RefreshError" in type_name and "google" in module:
            return True
        current = getattr(current, "__cause__", None)
    return False


def _extract_button_labels(reply_markup) -> list:
    """Extract button labels from an InlineKeyboardMarkup for logging."""
    if not reply_markup or not hasattr(reply_markup, "inline_keyboard"):
        return []
    labels = []
    for row in reply_markup.inline_keyboard:
        for button in row:
            labels.append(button.text)
    return labels


class TelegramNotificationService:
    """Handles notification sending, caption building, and keyboard construction.

    Uses the composition pattern -- receives a reference to the parent
    ``TelegramService`` for access to bot, repos, and settings.  This is
    the same pattern used by all other handler modules (commands,
    callbacks, autopost, settings, accounts).
    """

    def __init__(self, telegram_service):
        """
        Args:
            telegram_service: Parent TelegramService for access to
                bot, repos, and settings.
        """
        self.service = telegram_service

    async def send_notification(
        self, queue_item_id: str, force_sent: bool = False
    ) -> bool:
        """
        Send posting notification to Telegram channel.

        Args:
            queue_item_id: Queue item ID
            force_sent: Whether this was triggered by /next command

        Returns:
            True if sent successfully
        """
        from telegram import Bot

        # Initialize bot if not already done (for CLI usage)
        if self.service.bot is None:
            self.service.bot = Bot(token=self.service.bot_token)
            logger.debug("Telegram bot initialized for one-time use")

        queue_item = self.service.queue_repo.get_by_id(
            queue_item_id, chat_settings_id=SYSTEM_SCOPE
        )
        if not queue_item:
            logger.error(f"Queue item not found: {queue_item_id}")
            return False

        # Idempotency guard (claim-before-publish, mirrors #564): a stamped
        # telegram_message_id means the approval card is already in the chat.
        # Re-entry here (send retry, crash replay, /next re-fire) must not
        # post a duplicate card.
        if queue_item.telegram_message_id:
            logger.info(
                f"Queue item {queue_item_id} already has Telegram card "
                f"{queue_item.telegram_message_id} — skipping duplicate send"
            )
            return True

        media_item = self.service.media_repo.get_by_id(
            str(queue_item.media_item_id), chat_settings_id=SYSTEM_SCOPE
        )
        if not media_item:
            logger.error(f"Media item not found: {queue_item.media_item_id}")
            return False

        # Resolve the tenant that owns this queue item. Every per-chat
        # decision below (settings, account, source credentials, send
        # destination, stored chat id) must use the tenant's own chat,
        # never the deployment-wide TELEGRAM_CHANNEL_ID env chat.
        chat_settings = self._resolve_tenant_settings(queue_item)
        tenant_chat_id = chat_settings.telegram_chat_id
        verbose = self.service._is_verbose(tenant_chat_id, chat_settings=chat_settings)

        # Get active Instagram account for display
        active_account = self.service.ig_account_service.get_active_account(
            tenant_chat_id
        )

        # Build caption (pass queue_item for enhanced mode)
        caption = self._build_caption(
            media_item,
            queue_item,
            force_sent=force_sent,
            verbose=verbose,
            active_account=active_account,
            caption_style=chat_settings.caption_style or defaults.DEFAULT_CAPTION_STYLE,
        )

        # Get account count for keyboard cycle behavior
        account_count = self.service.ig_account_service.count_active_accounts(
            tenant_chat_id
        )

        # Build inline keyboard
        from src.services.core.telegram_utils import build_queue_action_keyboard

        reply_markup = build_queue_action_keyboard(
            queue_item_id,
            enable_instagram_api=chat_settings.enable_instagram_api,
            active_account=active_account,
            account_count=account_count,
            has_generated_caption=bool(
                media_item.generated_caption and not media_item.caption
            ),
        )

        try:
            # Get file bytes via provider (supports local and future cloud sources)
            import asyncio
            from io import BytesIO

            from src.services.media_sources.factory import MediaSourceFactory

            provider = MediaSourceFactory.get_provider_for_media_item(
                media_item, telegram_chat_id=tenant_chat_id
            )
            # Offload the blocking media download so it can't freeze the shared
            # bot event loop (which would stall the update poller, callbacks, and
            # the other loops) while this runs.
            file_bytes = await asyncio.to_thread(
                provider.download_file, media_item.source_identifier
            )

            photo_buffer = BytesIO(file_bytes)
            photo_buffer.name = media_item.file_name  # Telegram needs filename hint
            message = await self.service.bot.send_photo(
                chat_id=tenant_chat_id,
                photo=photo_buffer,
                caption=caption,
                reply_markup=reply_markup,
                parse_mode="Markdown",
            )

        except GoogleDriveAuthError:
            raise
        except ChatMigrated as e:
            # The chat migrated to a supergroup and changed id (#743). This is
            # the permanent send-path backstop — the only migration channel
            # that can still reach a chat whose live service message was
            # missed, which is every chat stranded before #744 shipped.
            #
            # It must not fall through to the blanket handler below. That
            # returned False, and the scheduler recorded the literal string
            # "send_notification returned False", so the new id — the one fact
            # a recovery pass needs — survived only in a log line and aged out
            # with log retention.
            #
            # Both ids are carried because neither alone is actionable:
            # telegram.error.ChatMigrated supplies only the new one, and the
            # old one is known only here, from the chat we addressed.
            raise ChatMigratedError(
                old_chat_id=tenant_chat_id, new_chat_id=e.new_chat_id
            ) from e
        except TimedOut as e:
            # Ambiguous delivery: Telegram may have posted the card even
            # though the response never arrived, so this must not read as a
            # clean retryable failure — a resend posts a duplicate card.
            raise AmbiguousDeliveryError(
                f"Telegram send timed out; the card may be delivered: {e}"
            ) from e
        except Exception as e:  # noqa: BLE001
            if _is_google_auth_error(e):
                raise GoogleDriveAuthError(
                    f"Google Drive token expired or revoked: {e}"
                ) from e
            logger.error(f"Failed to send Telegram notification: {e}")
            return False

        # The card is delivered from here on. Bookkeeping failures must not
        # turn into a send failure — a retry would post a duplicate card.
        # A lost stamp is healed at callback time (card reconciliation).
        try:
            # Save telegram message ID
            self.service.queue_repo.set_telegram_message(
                queue_item_id, message.message_id, tenant_chat_id
            )

            # Log outgoing bot response for visibility
            self.service.interaction_service.log_bot_response(
                response_type="photo_notification",
                context={
                    "caption": caption,
                    "buttons": _extract_button_labels(reply_markup),
                    "media_filename": media_item.file_name,
                    "queue_item_id": queue_item_id,
                    "force_sent": force_sent,
                },
                telegram_chat_id=tenant_chat_id,
                telegram_message_id=message.message_id,
            )
        except Exception as e:  # noqa: BLE001
            logger.error(
                f"Bookkeeping failed after delivered send for {queue_item_id} "
                f"(message {message.message_id}): {e}",
                exc_info=True,
            )

        logger.info(f"Sent Telegram notification for {media_item.file_name}")
        return True

    def _resolve_tenant_settings(self, queue_item):
        """Resolve the ChatSettings row of the tenant that owns a queue item.

        Falls back to the deployment-wide TELEGRAM_CHANNEL_ID chat only for
        legacy rows created before tenant stamping (chat_settings_id NULL)
        or when the referenced tenant row no longer exists.
        """
        if queue_item.chat_settings_id:
            chat_settings = self.service.settings_service.get_settings_by_id(
                str(queue_item.chat_settings_id)
            )
            if chat_settings:
                return chat_settings
            logger.warning(
                f"Queue item {queue_item.id} references missing chat_settings "
                f"{queue_item.chat_settings_id}; falling back to the global channel"
            )
        else:
            logger.warning(
                f"Queue item {queue_item.id} has no chat_settings_id (legacy row); "
                f"falling back to the global channel"
            )
        channel_settings = self.service.settings_service.get_settings(
            self.service.channel_id
        )
        if channel_settings is None:
            # Deployment misconfiguration, not a tenant condition: the global
            # channel row is provisioned by first contact. Refuse loudly (the
            # per-item send error handling contains it) rather than minting a
            # row from inside a delivery path (#842).
            raise TenantResolutionError(
                "unprovisioned_channel",
                f"global channel {self.service.channel_id} has no settings row",
            )
        return channel_settings

    def _build_caption(
        self,
        media_item,
        queue_item=None,
        force_sent: bool = False,
        verbose: bool = True,
        active_account=None,
        caption_style: Optional[str] = None,
    ) -> str:
        """Build caption for Telegram message with enhanced or simple formatting.

        `caption_style` is the per-chat preference (already resolved to a
        concrete value by the caller, falling back to the code default).
        Defaults to DEFAULT_CAPTION_STYLE for the few legacy callers that
        still build captions outside a chat context.
        """
        style = caption_style or defaults.DEFAULT_CAPTION_STYLE
        if style == "enhanced":
            return self._build_enhanced_caption(
                media_item,
                queue_item,
                force_sent=force_sent,
                verbose=verbose,
                active_account=active_account,
            )
        else:
            return self._build_simple_caption(
                media_item,
                force_sent=force_sent,
                verbose=verbose,
                active_account=active_account,
            )

    def _build_simple_caption(
        self,
        media_item,
        force_sent: bool = False,
        verbose: bool = True,
        active_account=None,
    ) -> str:
        """Build simple caption (original format)."""
        lines = []

        if force_sent:
            lines.append("⚡")

        if media_item.title:
            lines.append(f"📸 {_escape_md(media_item.title)}")

        if active_account:
            lines.append(f"📸 Account: {_escape_md(active_account.display_name)}")
        else:
            lines.append("📸 Account: Not set")

        if media_item.caption:
            lines.append(f"\n{_escape_md(media_item.caption)}")
        elif media_item.generated_caption:
            lines.append(f"\n🤖 {_escape_md(media_item.generated_caption)}")

        if media_item.link_url:
            lines.append(f"\n🔗 {media_item.link_url}")

        if media_item.tags:
            tags_str = " ".join([f"#{tag}" for tag in media_item.tags])
            lines.append(f"\n{tags_str}")

        if verbose:
            lines.append(f"\n📝 File: {_escape_md(media_item.file_name)}")
            lines.append(f"🆔 ID: {str(media_item.id)[:8]}")

        return "\n".join(lines)

    def _build_enhanced_caption(
        self,
        media_item,
        queue_item=None,
        force_sent: bool = False,
        verbose: bool = True,
        active_account=None,
    ) -> str:
        """Build enhanced caption with better formatting."""
        lines = []

        if force_sent:
            lines.append("⚡")

        if media_item.title:
            lines.append(f"📸 {_escape_md(media_item.title)}")

        if active_account:
            lines.append(f"📸 Account: {_escape_md(active_account.display_name)}")
        else:
            lines.append("📸 Account: Not set")

        if media_item.caption:
            lines.append(f"\n{_escape_md(media_item.caption)}")
        elif media_item.generated_caption:
            lines.append(f"\n🤖 {_escape_md(media_item.generated_caption)}")

        if media_item.link_url:
            lines.append(f"\n🔗 {media_item.link_url}")

        if media_item.tags:
            tags_str = " ".join([f"#{tag}" for tag in media_item.tags])
            lines.append(f"\n{tags_str}")

        # Verbose: debug info + workflow instructions.
        # The 3-step manual instructions are only useful when Auto Post is
        # unavailable. For accounts connected via Instagram Login the worker
        # publishes directly via the Graph API; the manual flow is dead
        # weight that clutters every card. Hide the steps for those accounts;
        # keep the file/ID debug lines whenever verbose is on so the user
        # still has identifiers in the message.
        if verbose:
            lines.append(f"\n📝 File: {media_item.file_name}")
            lines.append(f"🆔 ID: {str(media_item.id)[:8]}")

            has_auto_post = (
                active_account is not None
                and getattr(active_account, "auth_method", None) == "instagram_login"
            )
            if not has_auto_post:
                lines.append(f"\n{'━' * 20}")
                lines.append("1️⃣ Click & hold image → Save")
                lines.append('2️⃣ Tap "Open Instagram" below')
                lines.append("3️⃣ Post your story!")

        return "\n".join(lines)

    def _get_header_emoji(self, tags) -> str:
        """Get header emoji based on tags."""
        if not tags:
            return "📸"

        tags_lower = [tag.lower() for tag in tags]

        # Map tags to emojis
        if any(tag in tags_lower for tag in ["meme", "funny", "humor"]):
            return "😂"
        elif any(tag in tags_lower for tag in ["product", "shop", "store", "sale"]):
            return "🛍️"
        elif any(tag in tags_lower for tag in ["quote", "inspiration", "motivational"]):
            return "✨"
        elif any(tag in tags_lower for tag in ["announcement", "news", "update"]):
            return "📢"
        elif any(tag in tags_lower for tag in ["question", "poll", "interactive"]):
            return "💬"
        else:
            return "📸"
