"""Telegram callback handlers - admin handlers (batch approve, resume, reset)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from telegram import InlineKeyboardMarkup
from telegram.error import TelegramError

from src.services.core.queue_reap import reap_pending_rows
from src.utils.logger import logger
from src.utils.resilience import telegram_edit_with_retry
from datetime import datetime, timedelta, timezone

if TYPE_CHECKING:
    from src.services.core.telegram_callbacks_core import TelegramCallbackCore
    from src.services.core.telegram_service import TelegramService


class TelegramCallbackAdminHandlers:
    """Handles admin callback buttons in Telegram.

    Manages batch approve, resume, and reset callback flows.
    Uses composition: receives a TelegramService reference and a
    TelegramCallbackCore instance for shared utilities.
    """

    def __init__(self, service: TelegramService, core: TelegramCallbackCore):
        self.service = service
        self.core = core

    def _caller_chat_settings_id(self, query) -> str | None:
        """Resolve the calling chat's tenant id (chat_settings UUID), or None.

        Returns None when the chat has no settings row. Callers MUST treat
        None as "no tenant scope" and refuse to operate — never fall back to
        an unscoped query, which would reach across every tenant's rows.
        """
        chat_settings = self.service.settings_service.get_settings_if_exists(
            query.message.chat_id
        )
        return str(chat_settings.id) if chat_settings else None

    async def _require_caller_tenant(self, query) -> str | None:
        """Resolve the caller's tenant id, or warn and return None.

        Wraps :meth:`_caller_chat_settings_id` with the shared "no instance
        configured" reply the destructive resume/reset handlers use, so that
        bail message lives in one place.
        """
        cs_id = self._caller_chat_settings_id(query)
        if cs_id is None:
            await telegram_edit_with_retry(
                query.edit_message_text,
                "❌ No instance is configured for this chat.",
                parse_mode="Markdown",
            )
        return cs_id

    async def handle_batch_approve(self, data, user, query):
        """Handle batch_approve:{chat_settings_id} callback — approve all pending items.

        Marks each item as posted, creates history records, and applies
        repost-prevention locks. Processes sequentially so one failure
        doesn't affect others.
        """
        cs_id = data
        chat_id = query.message.chat_id

        # The queue belongs to the chat this callback fired in — not whatever
        # chat_settings_id the (forgeable) button data carries. Reject a foreign
        # or unresolvable tenant before touching any rows.
        caller_cs_id = self._caller_chat_settings_id(query)
        if caller_cs_id is None or caller_cs_id != cs_id:
            logger.warning(
                "batch_approve rejected: chat %s (cs=%s) does not own cs=%s",
                chat_id,
                caller_cs_id,
                cs_id,
            )
            await telegram_edit_with_retry(
                query.edit_message_text,
                "❌ Not authorized for this instance.",
                parse_mode="Markdown",
            )
            return

        try:
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([]))
        except TelegramError:
            pass

        pending = self.service.queue_repo.get_all_with_media(
            status="pending", chat_settings_id=cs_id
        )
        processing = self.service.queue_repo.get_all_with_media(
            status="processing", chat_settings_id=cs_id
        )
        all_items = pending + processing

        if not all_items:
            await telegram_edit_with_retry(
                query.edit_message_text,
                "📭 No pending items to approve.",
                parse_mode="Markdown",
            )
            return

        await telegram_edit_with_retry(
            query.edit_message_text,
            f"⏳ *Batch approving {len(all_items)} items...*",
            parse_mode="Markdown",
        )

        approved = 0
        failed = 0

        for queue_item, file_name, category in all_items:
            queue_id = str(queue_item.id)
            try:
                claimed = self.service.queue_repo.claim_for_processing(queue_id)
                if not claimed:
                    failed += 1
                    continue
                self.core._execute_complete_db_ops(
                    queue_id, claimed, user, "posted", True
                )
                approved += 1
            except Exception as e:  # noqa: BLE001
                logger.error(
                    f"Batch approve failed for {queue_id[:8]}: {type(e).__name__}: {e}"
                )
                failed += 1

        item_word = "item" if approved == 1 else "items"
        result_text = f"✅ *Batch Approve Complete*\n\n📤 {approved} {item_word} marked as posted\n"
        if failed > 0:
            fail_word = "item" if failed == 1 else "items"
            result_text += f"⚠️ {failed} {fail_word} failed\n"

        await telegram_edit_with_retry(
            query.edit_message_text,
            result_text,
            parse_mode="Markdown",
        )

        self.service.interaction_service.log_callback(
            user_id=str(user.id),
            callback_name="batch_approve",
            context={"approved": approved, "failed": failed},
            telegram_chat_id=chat_id,
            telegram_message_id=query.message.message_id,
        )

        logger.info(
            f"Batch approve by {self.service._get_display_name(user)}: "
            f"{approved} approved, {failed} failed"
        )

    async def handle_batch_approve_cancel(self, data, user, query):
        """Handle batch_approve_cancel callback — cancel batch approval."""
        await telegram_edit_with_retry(
            query.edit_message_text,
            "❌ *Batch approval cancelled.*",
            parse_mode="Markdown",
        )

    async def handle_resume_callback(self, action: str, user, query):
        """Handle resume callback buttons (reschedule/clear/force)."""
        try:
            await self._do_resume_callback(action, user, query)
        except Exception as e:  # noqa: BLE001
            logger.error(
                f"Failed to handle resume:{action}: {type(e).__name__}: {e}",
                exc_info=True,
            )
            await telegram_edit_with_retry(
                query.edit_message_text,
                "❌ Error during resume. Please try /settings.",
                parse_mode="Markdown",
            )

    async def _do_resume_callback(self, action: str, user, query):
        """Internal implementation of resume callback."""
        cs_id = await self._require_caller_tenant(query)
        if cs_id is None:
            return

        now = datetime.now(timezone.utc)
        all_pending = self.service.queue_repo.get_all(
            status="pending", chat_settings_id=cs_id
        )
        overdue = [p for p in all_pending if p.scheduled_for < now]

        if action == "reschedule":
            # Reschedule overdue posts to future times
            # Get time slots for rescheduling
            rescheduled = 0
            for i, item in enumerate(overdue):
                # Spread out over next few hours
                new_time = now + timedelta(hours=1 + (i * 0.5))
                self.service.queue_repo.update_scheduled_time(str(item.id), new_time)
                rescheduled += 1

            self.service.set_paused(False, user)
            await telegram_edit_with_retry(
                query.edit_message_text,
                f"📦 *Delivery ON*\n\n"
                f"🔄 Rescheduled {rescheduled} overdue posts.\n"
                f"First post in ~1 hour.",
                parse_mode="Markdown",
            )
            logger.info(
                f"Posting resumed by {self.service._get_display_name(user)}, "
                f"rescheduled {rescheduled} overdue posts"
            )

        elif action == "clear":
            # Clear all overdue posts. Route button-bearing rows through the
            # shared reap so live cards are expired (buttons stripped + terminal
            # history) rather than orphaned; button-less rows are plain-deleted.
            cleared = await reap_pending_rows(
                overdue,
                bot=self.service.bot,
                history_repo=self.service.history_repo,
                queue_repo=self.service.queue_repo,
            )

            self.service.set_paused(False, user)
            remaining = len(all_pending) - cleared
            await telegram_edit_with_retry(
                query.edit_message_text,
                f"📦 *Delivery ON*\n\n"
                f"🗑️ Cleared {cleared} overdue posts.\n"
                f"📊 {remaining} scheduled posts remaining.",
                parse_mode="Markdown",
            )
            logger.info(
                f"Posting resumed by {self.service._get_display_name(user)}, "
                f"cleared {cleared} overdue posts"
            )

        elif action == "force":
            # Resume without handling overdue - they'll be processed immediately
            self.service.set_paused(False, user)
            await telegram_edit_with_retry(
                query.edit_message_text,
                f"📦 *Delivery ON*\n\n"
                f"⚠️ {len(overdue)} overdue posts will be processed immediately.",
                parse_mode="Markdown",
            )
            logger.info(
                f"Posting resumed (force) by {self.service._get_display_name(user)}, "
                f"{len(overdue)} overdue posts"
            )

        # Log interaction
        self.service.interaction_service.log_callback(
            user_id=str(user.id),
            callback_name=f"resume:{action}",
            context={"overdue_count": len(overdue), "action": action},
            telegram_chat_id=query.message.chat_id,
            telegram_message_id=query.message.message_id,
        )

    async def handle_reset_callback(self, action: str, user, query):
        """Handle reset queue callback buttons (confirm/cancel).

        Legacy: kept for backward compat with old /reset confirmation messages.
        """
        try:
            if action == "confirm":
                # Reset queue — clear THIS chat's pending posts only.
                cs_id = await self._require_caller_tenant(query)
                if cs_id is None:
                    return
                all_pending = self.service.queue_repo.get_all(
                    status="pending", chat_settings_id=cs_id
                )
                # Route button-bearing rows through the shared reap so live
                # cards are expired rather than orphaned; plain-delete the rest.
                cleared = await reap_pending_rows(
                    all_pending,
                    bot=self.service.bot,
                    history_repo=self.service.history_repo,
                    queue_repo=self.service.queue_repo,
                )

                await telegram_edit_with_retry(
                    query.edit_message_text,
                    f"✅ *Queue Cleared*\n\n"
                    f"🗑️ Removed {cleared} pending posts.\n"
                    f"Media items remain in library.",
                    parse_mode="Markdown",
                )
                logger.info(
                    f"Queue cleared by {self.service._get_display_name(user)}: "
                    f"{cleared} posts removed"
                )

            elif action == "cancel":
                await telegram_edit_with_retry(
                    query.edit_message_text,
                    "❌ *Cancelled*\n\nQueue was not cleared.",
                    parse_mode="Markdown",
                )

            # Log interaction
            self.service.interaction_service.log_callback(
                user_id=str(user.id),
                callback_name=f"clear:{action}",
                context={"action": action},
                telegram_chat_id=query.message.chat_id,
                telegram_message_id=query.message.message_id,
            )
        except Exception as e:  # noqa: BLE001
            logger.error(
                f"Failed to handle reset:{action}: {type(e).__name__}: {e}",
                exc_info=True,
            )
            await telegram_edit_with_retry(
                query.edit_message_text,
                "❌ Error clearing queue. Please try again.",
                parse_mode="Markdown",
            )
