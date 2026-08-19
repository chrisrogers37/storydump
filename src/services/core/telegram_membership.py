"""Chat membership event handling for the Telegram bot.

Handles ChatMemberUpdated events: bot added to group (auto-link onboarding),
bot removed from group (deactivate memberships), and DM onboarding messages.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from src.repositories.chat_settings_repository import ChatIdMigrationConflict
from src.utils.logger import logger

if TYPE_CHECKING:
    from src.services.core.telegram_service import TelegramService


class TelegramMembershipHandler:
    """Handles bot membership changes and onboarding conversation flow."""

    def __init__(self, service: TelegramService):
        self.service = service

    async def handle_my_chat_member(self, update, context):
        """Handle ChatMemberUpdated events for the bot itself.

        Fires when the bot's membership status changes in any chat:
        - Bot added to group -> auto-link pending onboarding session
        - Bot kicked from group -> deactivate all memberships for that chat
        """
        member_update = update.my_chat_member
        if not member_update:
            return

        chat = member_update.chat
        if chat.type not in ("group", "supergroup"):
            return

        old_status = member_update.old_chat_member.status
        new_status = member_update.new_chat_member.status
        from_user = member_update.from_user

        if from_user is None:
            logger.info(
                f"Bot membership changed in group {chat.id} by anonymous admin "
                f"— skipping auto-link (use /link)"
            )
            return

        try:
            if new_status in ("member", "administrator") and old_status in (
                "left",
                "kicked",
            ):
                await self._handle_bot_added_to_group(chat, from_user)
            elif new_status in ("left", "kicked") and old_status in (
                "member",
                "administrator",
            ):
                self._handle_bot_removed_from_group(chat)
        except Exception as e:  # noqa: BLE001
            logger.error(
                f"my_chat_member handler error for chat {chat.id}: "
                f"{type(e).__name__}: {e}",
                exc_info=True,
            )
        finally:
            self.service.cleanup_transactions()

    async def _handle_bot_added_to_group(self, chat, from_user):
        """Bot was added to a group — check for pending onboarding session to auto-link."""
        from src.services.core.conversation_service import ConversationService

        user = self.service._get_or_create_user(from_user)

        # Race condition guard: the onboarding session may not be committed yet
        # if the user clicked "Add to Group" very quickly after naming.
        session = None
        for attempt in range(2):
            with ConversationService() as conv_service:
                session = conv_service.get_current_session(str(user.id))
            if session and session.step == "awaiting_group":
                break
            if attempt == 0:
                await asyncio.sleep(2)

        if not session or session.step != "awaiting_group":
            logger.info(
                f"Bot added to group {chat.id} by user {from_user.id} "
                f"— no pending onboarding session"
            )
            return

        with ConversationService() as conv_service:
            conv_service.link_group_to_instance(
                session=session,
                chat_id=chat.id,
                user_id=str(user.id),
                membership_repo=self.service.membership_repo,
            )

        name = session.pending_instance_name or "this group"
        logger.info(
            f"Auto-linked instance '{name}' to group {chat.id} "
            f"via my_chat_member (user {from_user.id})"
        )

        try:
            await self.service.bot.send_message(
                chat_id=from_user.id,
                text=(
                    f"✅ *{name}* is linked to your group!\n\n"
                    "Use /start here to manage your instances."
                ),
                parse_mode="Markdown",
            )
        except Exception:  # noqa: BLE001
            pass  # DM may be blocked

    def _handle_bot_removed_from_group(self, chat):
        """Bot was kicked from a group — deactivate all memberships."""
        # Deliberate #842 exception: teardown is idempotent — being removed
        # from a never-provisioned group has nothing to deactivate.
        chat_settings = self.service.settings_service.get_settings(chat.id)
        if not chat_settings:
            return

        count = self.service.membership_repo.deactivate_for_chat(str(chat_settings.id))
        evicted = self.service.user_manager.evict_memberships_for_chat(chat.id)

        logger.info(
            f"Bot removed from group {chat.id} — "
            f"deactivated {count} membership(s), evicted {evicted} cache entries"
        )

    async def handle_chat_migration(self, update, context):
        """Follow a group->supergroup migration (#743).

        Telegram changes a chat's id on migration and announces it with a
        service message carrying ``migrate_from_chat_id`` on the NEW chat. With
        no handler, the next update from the new id reaches ``get_or_create``,
        which finds nothing and mints a blank tenant while the real one strands
        on the dead id, silently.

        Telegram delivers this update twice; the repository call is idempotent,
        so the duplicate is a no-op rather than an error.
        """
        message = update.effective_message
        if not message or not message.migrate_from_chat_id:
            return

        old_chat_id = message.migrate_from_chat_id
        new_chat_id = message.chat.id

        try:
            migrated = self.service.settings_service.migrate_chat_id(
                old_chat_id, new_chat_id
            )
        except ChatIdMigrationConflict:
            # Both ids hold a tenant. Refusing is deliberate — see the
            # repository. Loud, because it needs a human to merge them.
            logger.error(
                "chat migration %s -> %s BLOCKED: a tenant already exists at the "
                "new id. Settings for the original chat are stranded and need "
                "manual reconciliation.",
                old_chat_id,
                new_chat_id,
            )
            return

        if migrated is None:
            logger.info(
                "chat migration %s -> %s: nothing to migrate (no settings for "
                "the old id)",
                old_chat_id,
                new_chat_id,
            )

    async def handle_onboarding_message(self, update, context):
        """Handle text input during DM onboarding (naming step)."""
        from src.services.core.conversation_service import ConversationService

        session_id = context.user_data.get("onboarding_session_id")
        if not session_id:
            return

        with ConversationService() as conv_service:
            session = conv_service.get_session_by_id(session_id)

        if not session or session.step == "complete":
            context.user_data.pop("onboarding_session_id", None)
            return

        if session.step == "naming":
            instance_name = update.message.text.strip()[:100]
            if not instance_name:
                await update.message.reply_text(
                    "Please enter a name for your instance."
                )
                return

            with ConversationService() as conv_service:
                conv_service.set_instance_name(str(session.id), instance_name)

            bot_username = (await self.service.bot.get_me()).username
            startgroup_url = (
                f"https://t.me/{bot_username}?startgroup=setup_{session.id}"
            )

            from telegram import InlineKeyboardButton, InlineKeyboardMarkup

            keyboard = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("Add to Group Chat", url=startgroup_url)],
                ]
            )
            await update.message.reply_text(
                f"Great! *{instance_name}* it is.\n\n"
                "Now add me to the group chat where your team will review posts.\n\n"
                "Bot already in your group? Run `/link` in that group.",
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
        elif session.step == "awaiting_group":
            await update.message.reply_text(
                "Still waiting to link your group — add me to the group chat, "
                "or run `/link` in that group if I'm already there.",
                parse_mode="Markdown",
            )
