"""Telegram service - thin orchestrator for bot operations.

TelegramService owns the bot lifecycle (init, polling, shutdown) and
coordinates between focused handler classes. All domain logic lives in
the extracted handler modules:

- telegram_operation_state.py  — operation locks and cancel flags
- telegram_user_manager.py     — user creation, membership, display names
- telegram_membership.py       — bot added/removed from groups, onboarding
- telegram_lifecycle.py        — startup/shutdown admin notifications
- telegram_notification.py     — queue item notifications and captions
- telegram_commands.py         — /command handlers
- telegram_callbacks*.py       — inline button callback handlers
- telegram_autopost.py         — Instagram auto-post flow
- telegram_settings.py         — settings UI handlers
- telegram_accounts.py         — account management handlers
"""

import asyncio

from telegram import Bot, BotCommand
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from src.services.base_service import BaseService
from src.repositories.user_repository import UserRepository
from src.repositories.queue_repository import QueueRepository
from src.repositories.history_repository import HistoryRepository
from src.repositories.media_repository import MediaRepository
from src.repositories.lock_repository import LockRepository
from src.services.core.media_lock import MediaLockService
from src.services.core.interaction_service import InteractionService
from src.services.core.settings_service import SettingsService
from src.services.core.instagram_account_service import InstagramAccountService
from src.services.core.telegram_notification import TelegramNotificationService
from src.services.core.telegram_operation_state import OperationStateManager
from src.services.core.telegram_user_manager import TelegramUserManager
from src.repositories.membership_repository import MembershipRepository
from src.config.settings import settings
from src.utils.logger import logger

# Re-export for any remaining external imports
from src.services.core.telegram_utils import escape_markdown as _escape_markdown  # noqa: F401

# Handler classes are imported inside initialize() to avoid circular imports.


class TelegramService(BaseService):
    """Thin orchestrator for Telegram bot operations.

    Owns the bot lifecycle and coordinates between extracted handler classes.
    Domain logic lives in the handler modules listed in the module docstring.
    """

    def __init__(self):
        super().__init__()
        self.bot_token = settings.TELEGRAM_BOT_TOKEN
        self.channel_id = settings.TELEGRAM_CHANNEL_ID
        self.admin_chat_id = settings.ADMIN_TELEGRAM_CHAT_ID
        self.user_repo = UserRepository()
        self.queue_repo = QueueRepository()
        self.history_repo = HistoryRepository()
        self.media_repo = MediaRepository()
        self.lock_repo = LockRepository()
        self.lock_service = MediaLockService()
        self.interaction_service = InteractionService()
        self.settings_service = SettingsService()
        self.ig_account_service = InstagramAccountService()
        self.membership_repo = MembershipRepository()
        self.bot = None
        self.application = None
        # Extracted sub-components
        self.operation_state = OperationStateManager()
        self.user_manager = TelegramUserManager(self)
        self.notification_service = TelegramNotificationService(self)
        self._callback_dispatch: dict = {}

    # ------------------------------------------------------------------
    # Delegate methods — preserve the public API so handler modules can
    # continue accessing these through self.service.* unchanged.
    # ------------------------------------------------------------------

    def get_operation_lock(self, queue_id: str) -> asyncio.Lock:
        """Delegate to OperationStateManager."""
        return self.operation_state.get_lock(queue_id)

    def get_cancel_flag(self, queue_id: str) -> asyncio.Event:
        """Delegate to OperationStateManager."""
        return self.operation_state.get_cancel_flag(queue_id)

    def cleanup_operation_state(self, queue_id: str):
        """Delegate to OperationStateManager."""
        self.operation_state.cleanup(queue_id)

    def _get_or_create_user(self, telegram_user, telegram_chat_id=None):
        """Delegate to TelegramUserManager."""
        return self.user_manager.get_or_create_user(telegram_user, telegram_chat_id)

    def _get_display_name(self, user) -> str:
        """Delegate to TelegramUserManager."""
        return self.user_manager.get_display_name(user)

    def _is_verbose(self, chat_id, chat_settings=None) -> bool:
        """Check if verbose notifications are enabled for a chat."""
        if chat_settings is None:
            chat_settings = self.settings_service.get_settings(chat_id)
        if chat_settings.show_verbose_notifications is not None:
            return chat_settings.show_verbose_notifications
        return True

    # ------------------------------------------------------------------
    # Lifecycle overrides (BaseService)
    # ------------------------------------------------------------------

    def _extra_repositories(self):
        """Include InteractionService's repo in every session-lifecycle op.

        InteractionService does not extend BaseService (by design — it's
        fire-and-forget logging that doesn't need execution tracking), so the
        base attribute walk skips it. Surfacing it here means cleanup, detach,
        and close all cover it — instead of a special-cased override per op —
        preventing "idle in transaction" leaks and keeping its session isolated
        per task under concurrent dispatch.
        """
        svc = getattr(self, "interaction_service", None)
        if svc is None:
            return ()
        return (svc.interaction_repo,)

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    async def initialize(self):
        """Initialize Telegram bot and register all handlers."""
        self.bot = Bot(token=self.bot_token)
        # concurrent_updates: process button taps concurrently (each in its own
        # asyncio Task) so a slow in-flight callback no longer serializes every
        # other user's tap behind it. Bounded — each concurrent callback holds its
        # own per-task DB session, so the bound keeps peak DB connections within
        # the pool (see settings.TELEGRAM_MAX_CONCURRENT_UPDATES).
        self.application = (
            Application.builder()
            .token(self.bot_token)
            .concurrent_updates(settings.TELEGRAM_MAX_CONCURRENT_UPDATES)
            .build()
        )

        # Initialize sub-handlers (after bot/application are created)
        from src.services.core.telegram_commands import TelegramCommandHandlers
        from src.services.core.telegram_callbacks import TelegramCallbackHandlers
        from src.services.core.telegram_autopost import TelegramAutopostHandler
        from src.services.core.telegram_settings import TelegramSettingsHandlers
        from src.services.core.telegram_accounts import TelegramAccountHandlers
        from src.services.core.start_command_router import StartCommandRouter
        from src.services.core.telegram_membership import TelegramMembershipHandler
        from src.services.core.telegram_lifecycle import TelegramLifecycleHandler

        self.commands = TelegramCommandHandlers(self)
        self.callbacks = TelegramCallbackHandlers(self)
        self.autopost = TelegramAutopostHandler(self)
        self.settings_handler = TelegramSettingsHandlers(self)
        self.accounts = TelegramAccountHandlers(self)
        self.start_router = StartCommandRouter(self)
        self.membership_handler = TelegramMembershipHandler(self)
        self.lifecycle = TelegramLifecycleHandler(self)

        # Build callback dispatch table (must be after handlers are initialized)
        self._callback_dispatch = self._build_callback_dispatch_table()

        # Register command handlers
        command_map = {
            # Active commands
            "start": self.commands.handle_start,
            "status": self.commands.handle_status,
            "next": self.commands.handle_next,
            "cleanup": self.commands.handle_cleanup,
            "approveall": self.commands.handle_approveall,
            "help": self.commands.handle_help,
            "settings": self.settings_handler.handle_settings,
            "setup": self.settings_handler.handle_settings,
            # Multi-account commands
            "link": self.commands.handle_link,
            "name": self.commands.handle_name,
            "instances": self.commands.handle_instances,
            "new": self.commands.handle_new,
            # Retired commands (show helpful redirect)
            "queue": self.commands.handle_removed_command,
            "pause": self.commands.handle_removed_command,
            "resume": self.commands.handle_removed_command,
            "history": self.commands.handle_removed_command,
            "sync": self.commands.handle_removed_command,
            "schedule": self.commands.handle_removed_command,
            "stats": self.commands.handle_removed_command,
            "locks": self.commands.handle_removed_command,
            "reset": self.commands.handle_removed_command,
            "dryrun": self.commands.handle_removed_command,
            "backfill": self.commands.handle_removed_command,
            "connect": self.commands.handle_removed_command,
        }
        for cmd, handler in command_map.items():
            self.application.add_handler(CommandHandler(cmd, handler))

        # Register callback and message handlers
        self.application.add_handler(CallbackQueryHandler(self._handle_callback))
        self.application.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND, self._handle_conversation_message
            )
        )
        self.application.add_handler(
            ChatMemberHandler(
                self.membership_handler.handle_my_chat_member,
                ChatMemberHandler.MY_CHAT_MEMBER,
            )
        )

        # Register commands with Telegram for autocomplete menu
        commands = [
            BotCommand("start", "Open Storydump (setup & config)"),
            BotCommand("status", "System health & media overview"),
            BotCommand("setup", "Quick settings & toggles"),
            BotCommand("next", "Send next post now"),
            BotCommand("approveall", "Approve all pending posts"),
            BotCommand("instances", "List your instances (DM)"),
            BotCommand("new", "Create a new instance (DM)"),
            BotCommand("name", "Set instance name (group)"),
            BotCommand("link", "Link group to pending instance"),
            BotCommand("cleanup", "Delete recent bot messages"),
            BotCommand("help", "Show available commands"),
        ]
        await self.bot.set_my_commands(commands)

        logger.info("Telegram bot initialized with command menu")

    # ------------------------------------------------------------------
    # Callback dispatch (orchestration — stays here)
    # ------------------------------------------------------------------

    def _build_callback_dispatch_table(self) -> dict:
        """Build the callback action dispatch table.

        Returns a dictionary mapping action strings to handler coroutines.
        All handlers in this table use the standard (data, user, query) signature.

        Actions that need special signatures (context, sub-routing, etc.) are
        NOT in this table -- they are handled in _handle_callback_special_cases().
        """
        return {
            # Queue item actions (telegram_callbacks.py)
            "posted": self.callbacks.handle_posted,
            "skip": self.callbacks.handle_skipped,
            "back": self.callbacks.handle_back,
            "reject": self.callbacks.handle_reject_confirmation,
            "confirm_reject": self.callbacks.handle_rejected,
            "cancel_reject": self.callbacks.handle_cancel_reject,
            "regenerate_caption": self.callbacks.handle_regenerate_caption,
            "resume": self.callbacks.handle_resume_callback,
            "clear": self.callbacks.handle_reset_callback,  # Legacy name for reset
            "batch_approve": self.callbacks.handle_batch_approve,
            "batch_approve_cancel": self.callbacks.handle_batch_approve_cancel,
            # Auto-post (telegram_autopost.py)
            "autopost": self.autopost.handle_autopost,
            # Settings (telegram_settings.py)
            "settings_toggle": self.settings_handler.handle_settings_toggle,
            "schedule_action": self.settings_handler.handle_schedule_action,
            "schedule_confirm": self.settings_handler.handle_schedule_confirm,
            # Instance management (telegram_settings.py)
            "instance_manage": self.settings_handler.handle_instance_manage,
            # Account management (telegram_accounts.py)
            "switch_account": self.accounts.handle_account_switch,
            "account_remove": self.accounts.handle_account_remove_confirm,
            "account_remove_confirmed": self.accounts.handle_account_remove_execute,
            "select_account": self.accounts.handle_post_account_selector,
            "cycle_account": self.accounts.handle_cycle_account,
            "sap": self.accounts.handle_post_account_switch,
            "btp": self.accounts.handle_back_to_post,
        }

    async def _handle_callback_special_cases(self, action, data, user, query, context):
        """Handle callback actions that need special signatures or sub-routing.

        Returns True if the action was handled, False if not recognized.
        """
        if action == "settings_refresh":
            await self.settings_handler.refresh_settings_message(query)
            return True

        elif action == "settings_edit":
            await self.settings_handler.handle_settings_edit_start(
                data, user, query, context
            )
            return True

        elif action == "settings_edit_cancel":
            await self.settings_handler.handle_settings_edit_cancel(query, context)
            return True

        elif action == "settings_close":
            await self.settings_handler.handle_settings_close(query)
            return True

        elif action == "settings_accounts":
            if data == "select":
                await self.accounts.handle_account_selection_menu(user, query)
            elif data == "back":
                await self.settings_handler.refresh_settings_message(query)
            return True

        elif action == "accounts_config":
            if data == "add":
                await self.accounts.handle_add_account_via_webapp(user, query)
            elif data == "remove":
                await self.accounts.handle_remove_account_menu(user, query)
            elif data == "noop":
                await query.answer()
            return True

        elif action == "instance_new":
            if query.message.chat.type != "private":
                await query.answer("Use this in a DM with me.", show_alert=True)
                return True

            from src.services.core.conversation_service import ConversationService

            with ConversationService() as conv_service:
                session = conv_service.start_onboarding(str(user.id))

            context.user_data["onboarding_session_id"] = str(session.id)

            await query.edit_message_text(
                "Let's set up a new instance!\n\n"
                "What do you want to call it?\n"
                '(e.g. "TL Enterprises", "Personal Brand")'
            )

            self.interaction_service.log_callback(
                user_id=str(user.id),
                callback_name="instance_new",
                telegram_chat_id=query.message.chat_id,
                telegram_message_id=query.message.message_id,
            )
            return True

        return False

    async def _handle_callback(self, update, context):
        """Handle inline button callbacks.

        Uses a two-tier dispatch approach:
        1. Dictionary lookup for standard (data, user, query) handlers
        2. Special-case method for handlers with non-standard signatures or sub-routing
        """
        from src.utils.resilience import log_pool_status

        query = update.callback_query
        try:
            logger.info(f"📞 Callback received: {query.data}")
            log_pool_status()

            parts = query.data.split(":", 1)
            action = parts[0]
            data = parts[1] if len(parts) > 1 else None

            logger.info(f"📞 Parsed action='{action}', data='{data}'")

            try:
                chat_id = int(query.message.chat_id) if query.message else None
            except (TypeError, ValueError):
                chat_id = None
            user = self._get_or_create_user(query.from_user, telegram_chat_id=chat_id)

            # Tier 1: Standard dispatch (data, user, query) handlers
            handler = self._callback_dispatch.get(action)
            if handler:
                await handler(data, user, query)
                await self._fallback_answer(query)
                return

            # Tier 2: Special cases (non-standard signatures, sub-routing)
            handled = await self._handle_callback_special_cases(
                action, data, user, query, context
            )
            if handled:
                await self._fallback_answer(query)
                return

            logger.warning(f"Unknown callback action: {action}")
            await self._fallback_answer(query)

        except Exception as e:  # noqa: BLE001
            logger.error(
                f"Unhandled error in callback '{query.data}': {type(e).__name__}: {e}",
                exc_info=True,
            )
            try:
                await query.answer(
                    "⚠️ Something went wrong. Please try again.",
                    show_alert=True,
                )
            except Exception:  # noqa: BLE001
                pass

        finally:
            self.cleanup_transactions()

    @staticmethod
    async def _fallback_answer(query) -> None:
        """Stop the button spinner if the handler didn't answer the query.

        Telegram accepts one answer per callback query, so answering is left
        to handlers (their toasts and show_alert popups must be the first —
        and thus visible — answer). For handlers that don't answer, this
        blank answer stops the spinner; if the handler already answered,
        Telegram rejects this one and the rejection is swallowed.
        """
        try:
            await query.answer()
        except Exception:  # noqa: BLE001
            logger.debug("Callback already answered or stale — fallback skipped")

    # ------------------------------------------------------------------
    # Conversation routing
    # ------------------------------------------------------------------

    async def _handle_conversation_message(self, update, context):
        """Route text messages to the appropriate conversation handler."""
        if "settings_edit_state" in context.user_data:
            handled = await self.settings_handler.handle_settings_edit_message(
                update, context
            )
            if handled:
                return

        if (
            update.effective_chat.type == "private"
            and "onboarding_session_id" in context.user_data
        ):
            await self.membership_handler.handle_onboarding_message(update, context)
            return

    # ------------------------------------------------------------------
    # Notification delegates
    # ------------------------------------------------------------------

    async def send_notification(
        self, queue_item_id: str, force_sent: bool = False
    ) -> bool:
        """Delegate to notification service."""
        return await self.notification_service.send_notification(
            queue_item_id, force_sent=force_sent
        )

    def _build_caption(
        self,
        media_item,
        queue_item=None,
        force_sent: bool = False,
        verbose: bool = True,
        active_account=None,
    ) -> str:
        """Delegate to notification service."""
        return self.notification_service._build_caption(
            media_item,
            queue_item,
            force_sent=force_sent,
            verbose=verbose,
            active_account=active_account,
        )

    async def send_startup_notification(self):
        """Delegate to lifecycle handler."""
        await self.lifecycle.send_startup_notification()

    async def send_shutdown_notification(
        self, uptime_seconds: int = 0, posts_sent: int = 0
    ):
        """Delegate to lifecycle handler."""
        await self.lifecycle.send_shutdown_notification(uptime_seconds, posts_sent)

    # ------------------------------------------------------------------
    # Polling lifecycle
    # ------------------------------------------------------------------

    async def start_polling(self):
        """Start bot polling."""
        logger.info("Starting Telegram bot polling...")

        async def _error_handler(update, context):
            logger.error(
                f"Telegram handler error: {context.error}",
                exc_info=context.error,
            )

        self.application.add_error_handler(_error_handler)

        await self.application.initialize()
        await self.application.start()
        await self.application.updater.start_polling(
            allowed_updates=[
                "message",
                "callback_query",
                "my_chat_member",
            ],
            drop_pending_updates=True,
        )
        logger.info("Telegram bot polling started successfully")

        stop_event = asyncio.Event()
        await stop_event.wait()

    async def stop_polling(self):
        """Stop bot polling."""
        logger.info("Stopping Telegram bot polling...")
        await self.application.updater.stop()
        await self.application.stop()
        await self.application.shutdown()
