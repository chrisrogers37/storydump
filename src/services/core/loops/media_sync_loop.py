"""Media sync loop — reconciles provider files with database on schedule."""

import asyncio

from src.config.settings import settings
from src.services.core.loops.heartbeat import record_heartbeat
from src.services.core.loops.lifecycle import session_state
from src.services.core.media_sync import MediaSyncService
from src.utils.logger import logger


def _sync_isolated(sync_service: MediaSyncService, **sync_kwargs):
    """Run ``sync_service.sync`` in a worker thread with isolated, self-cleaned sessions.

    Called via ``asyncio.to_thread`` (which copies the loop task's context). We
    detach first so this thread opens FRESH sessions rather than sharing the
    loop's, then end them in-thread. Because those sessions live only in this
    thread's context, the ``transaction_cleanup_loop``'s periodic
    ``cleanup_transactions`` running in the main-loop context cannot see — and so
    cannot race — the session ``sync`` is using. (That race is why a naive offload
    was unsafe: sync_service is a singleton whose session that loop commits or
    replaces every 30s.)
    """
    sync_service.begin_isolated_transactions()
    try:
        return sync_service.sync(**sync_kwargs)
    finally:
        sync_service.cleanup_transactions()


async def media_sync_loop(
    sync_service: MediaSyncService,
    settings_service=None,
    telegram_service=None,
):
    """Run media sync loop - reconcile provider files with database on schedule.

    Iterates over all tenants with media_sync_enabled=True and syncs each
    independently. Falls back to global env var behavior if no tenants
    have sync enabled.

    Args:
        sync_service: The MediaSyncService instance
        settings_service: SettingsService for tenant discovery
        telegram_service: Optional TelegramService for error notifications
    """
    logger.info(
        f"Starting media sync loop (interval: {settings.MEDIA_SYNC_INTERVAL_SECONDS}s, "
        "source: per-chat)"
    )

    # Track consecutive failures for notification suppression
    consecutive_failures = 0
    last_error_notified = None

    while True:
        record_heartbeat("media_sync")
        try:
            # Multi-tenant: sync each tenant with media_sync_enabled=True
            sync_enabled_chats = []
            if settings_service:
                sync_enabled_chats = settings_service.get_all_sync_enabled_chats()

            if sync_enabled_chats:
                for chat in sync_enabled_chats:
                    try:
                        result = await asyncio.to_thread(
                            _sync_isolated,
                            sync_service,
                            telegram_chat_id=chat.telegram_chat_id,
                            triggered_by="scheduler",
                        )

                        if result.total_processed > 0 or result.errors > 0:
                            logger.info(
                                f"[chat={chat.telegram_chat_id}] "
                                f"Media sync completed: "
                                f"{result.new} new, {result.updated} updated, "
                                f"{result.deactivated} deactivated, "
                                f"{result.reactivated} reactivated, "
                                f"{result.errors} errors"
                            )
                    except Exception as e:
                        logger.error(
                            f"[chat={chat.telegram_chat_id}] Media sync error: {e}",
                            exc_info=True,
                        )
            else:
                # Legacy fallback: single-tenant using global env vars
                result = await asyncio.to_thread(
                    _sync_isolated, sync_service, triggered_by="scheduler"
                )

                if result.total_processed > 0 or result.errors > 0:
                    logger.info(
                        f"Media sync completed: "
                        f"{result.new} new, {result.updated} updated, "
                        f"{result.deactivated} deactivated, "
                        f"{result.reactivated} reactivated, "
                        f"{result.errors} errors"
                    )

            # Signal scheduler that media state is now known
            if not session_state.initial_sync_complete:
                session_state.initial_sync_complete = True
                logger.info("Initial media sync complete — scheduler gate open")

            # Reset failure counter on success
            consecutive_failures = 0
            last_error_notified = None

        except Exception as e:
            logger.error(f"Error in media sync loop: {e}", exc_info=True)

            consecutive_failures += 1
            error_str = str(e)

            if telegram_service and (
                consecutive_failures == 1 or error_str != last_error_notified
            ):
                last_error_notified = error_str
                await _notify_sync_error(
                    telegram_service,
                    f"\U0001f534 *Media Sync Failed*\n\n"
                    f"Error: `{type(e).__name__}`\n"
                    f"Details: {str(e)[:200]}\n\n"
                    f"Consecutive failures: {consecutive_failures}\n"
                    f"Sync will retry in {settings.MEDIA_SYNC_INTERVAL_SECONDS}s.",
                )

        finally:
            try:
                sync_service.cleanup_transactions()
            except Exception as cleanup_err:
                logger.warning(
                    f"cleanup_transactions failed for MediaSyncService: {cleanup_err}"
                )
            if settings_service:
                try:
                    settings_service.cleanup_transactions()
                except Exception as cleanup_err:
                    logger.warning(
                        f"cleanup_transactions failed for SettingsService: {cleanup_err}"
                    )

        await asyncio.sleep(settings.MEDIA_SYNC_INTERVAL_SECONDS)


async def _notify_sync_error(telegram_service, message: str):
    """Send sync error notification to the admin chat, suppressing errors.

    Loop-level sync failures are deployment-level operational alerts, so
    they go to ADMIN_TELEGRAM_CHAT_ID -- not to whichever tenant chat the
    TELEGRAM_CHANNEL_ID env var happens to point at. Rate limiting is the
    caller's job (it notifies on the first failure or a new error only).
    """
    try:
        await telegram_service.bot.send_message(
            chat_id=telegram_service.admin_chat_id,
            text=message,
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.warning(f"Failed to send sync error notification: {e}")
