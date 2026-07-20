"""JIT scheduler loop — checks for due posting slots every minute.

Also runs hourly retention cleanup, pool health checks, and token
health checks as sub-tasks within the scheduler tick cycle.
"""

import asyncio
from datetime import datetime, timezone
from time import time

from src.exceptions.google_drive import GoogleDriveAuthError
from src.repositories.queue_repository import QueueRepository
from src.repositories.service_run_repository import ServiceRunRepository
from src.services.core.health_check import HealthCheckService
from src.services.core.loops.heartbeat import record_heartbeat
from src.services.core.loops.lifecycle import session_state
from src.services.core.loops.periodic import PeriodicScheduler
from src.services.core.posting import PostingService
from src.services.core.queue_reap import expire_sent_row
from src.services.core.scheduler import SchedulerService
from src.exceptions.instagram import TokenCorruptError, TokenRevokedError
from src.services.integrations.token_refresh import TokenRefreshService
from src.utils.logger import logger

# Retention policy: delete service_runs older than 7 days
SERVICE_RUNS_RETENTION_DAYS = 7
# Periodic sub-task intervals, in seconds of wall-clock time. Each sub-task is
# gated on a durable last-run timestamp (PeriodicScheduler), NOT an in-memory
# tick counter, so the interval survives process restarts — the #547 fix.
RETENTION_INTERVAL_SECONDS = 3600  # hourly
POOL_CHECK_INTERVAL_SECONDS = 3600  # hourly (pool depletion + Drive-token health)
TOKEN_REFRESH_INTERVAL_SECONDS = 86400  # daily
# Throttle pool alerts to once per 24h per chat
POOL_ALERT_COOLDOWN_SECONDS = 86400

# Durable periodic sub-task keys (stored as the marker rows' method_name).
TASK_RETENTION = "retention"
TASK_HEALTH_CHECKS = "health_checks"
TASK_TOKEN_REFRESH = "token_refresh"


async def _scheduler_tick(
    scheduler_service: SchedulerService,
    posting_service: PostingService,
    settings_service,
    queue_repo: QueueRepository,
    *,
    first_tick: bool = False,
) -> list:
    """Process one scheduler tick: discard stale queue items, process due slots.

    Args:
        first_tick: True on the first tick after worker startup.
            Passed to process_slot so catch-up posts reset to now
            instead of advancing gradually.

    Returns the list of active chats discovered this tick (used by health
    checks), or None when the tick short-circuited on the initial-sync wait
    before reaching chat processing. The caller uses None to know the tick did
    NOT process chats, so the first-tick catch-up flag is not consumed by a
    sync-wait no-op tick (#553).
    """
    # Gracefully expire button-bearing rows stuck in 'processing' past reap
    # age: their inline buttons are stripped and a terminal 'expired' history
    # row is written, so a late tap shows "Expired" instead of the scary
    # "Queue item not found". There is no raw-delete sweep any more: an
    # unstamped stale row is parked by resolve_stale_processing (10 min) and
    # the hourly cleanup loop deletes aged rows through the history-first
    # reap (#687) — a raw delete here could orphan a maybe-delivered card.
    #
    # queue_repo is a standalone repository (not owned by a BaseService), so
    # the outer loop's cleanup_transactions() doesn't roll it back on error.
    # Without this guard a single failed query would leave the session in a
    # broken transaction and every subsequent tick would PendingRollbackError
    # for the lifetime of the worker — observed in production.
    try:
        telegram_service = scheduler_service.telegram_service
        if telegram_service is not None:
            bot = telegram_service.application.bot
            history_repo = scheduler_service.history_repo
            for row in queue_repo.get_stale_sent(hours=24, status="processing"):
                await expire_sent_row(
                    row, bot=bot, history_repo=history_repo, queue_repo=queue_repo
                )
    except Exception:
        queue_repo.rollback()
        raise

    # Wait for at least one media sync to complete before posting.
    # Posting against a stale or empty media_items table leads to
    # "no eligible media" failures or wrong selections.
    if not session_state.initial_sync_complete:
        logger.debug("Scheduler tick: waiting for initial media sync")
        return None

    if settings_service:
        active_chats = settings_service.get_all_active_chats()
    else:
        active_chats = []

    if not active_chats:
        # Throttle to once per 10 minutes (every 10th tick) to avoid log spam
        _no_active_chats_tick_count = (
            getattr(_scheduler_tick, "_no_active_ticks", 0) + 1
        )
        _scheduler_tick._no_active_ticks = _no_active_chats_tick_count
        if _no_active_chats_tick_count == 1 or _no_active_chats_tick_count % 10 == 0:
            logger.warning(
                "Scheduler tick: no active chats found "
                "(check onboarding_completed / active_instagram_account_id)"
            )
    else:
        _scheduler_tick._no_active_ticks = 0

    if active_chats:
        for chat in active_chats:
            chat_id = chat.telegram_chat_id
            try:
                result = await scheduler_service.process_slot(
                    telegram_chat_id=chat_id, first_tick=first_tick
                )

                if result.get("posted"):
                    session_state.posts_sent += 1
                    logger.info(
                        f"[chat={chat_id}] "
                        f"Posted: {result.get('media_file', '?')} "
                        f"[{result.get('category', '?')}]"
                    )

                    # Send quiet notification for auto-approved items
                    if (
                        result.get("auto_approved")
                        and scheduler_service.telegram_service
                    ):
                        try:
                            bot = scheduler_service.telegram_service.application.bot
                            await bot.send_message(
                                chat_id=chat_id,
                                text=(
                                    f"\u2705 Auto-approved: "
                                    f"{result.get('media_file', '?')} "
                                    f"[{result.get('category', '?')}]"
                                ),
                            )
                        except Exception:
                            pass

            except GoogleDriveAuthError:
                logger.error(
                    f"Google Drive auth error for chat {chat_id}",
                    exc_info=True,
                )
                await posting_service.send_gdrive_auth_alert(chat_id)

            except Exception as e:
                logger.error(
                    f"Error processing chat {chat_id}: {e}",
                    exc_info=True,
                )

    return active_chats


async def _retention_cleanup_tick(
    service_run_repo: ServiceRunRepository,
) -> None:
    """Purge old service_runs to prevent table bloat."""
    try:
        deleted = service_run_repo.delete_older_than(SERVICE_RUNS_RETENTION_DAYS)
        if deleted > 0:
            logger.info(
                f"Retention: deleted {deleted} service_runs older than "
                f"{SERVICE_RUNS_RETENTION_DAYS} days"
            )
    except Exception as e:
        logger.warning(f"Service runs retention cleanup failed: {e}")
    finally:
        try:
            service_run_repo.end_read_transaction()
        except Exception as cleanup_err:
            logger.warning(
                f"cleanup_transactions failed for ServiceRunRepository: {cleanup_err}"
            )


async def _pool_health_tick(
    active_chats: list,
    scheduler_service: SchedulerService,
    health_check_service: HealthCheckService,
    pool_alert_last_sent: dict[int, float],
) -> None:
    """Check media pool depletion and send alerts for low categories."""
    try:
        if active_chats and scheduler_service.telegram_service:
            now = time()
            bot = scheduler_service.telegram_service.application.bot
            # Prune stale entries for chats no longer active
            active_ids = {c.telegram_chat_id for c in active_chats}
            for stale_id in set(pool_alert_last_sent) - active_ids:
                del pool_alert_last_sent[stale_id]

            for chat in active_chats:
                chat_id = chat.telegram_chat_id
                if (
                    now - pool_alert_last_sent.get(chat_id, 0)
                    < POOL_ALERT_COOLDOWN_SECONDS
                ):
                    continue

                pool_info = health_check_service.check_media_pool_for_chat(
                    chat_id, chat_settings=chat
                )
                alert_text = health_check_service.format_pool_alert(pool_info)
                if alert_text:
                    await bot.send_message(chat_id=chat_id, text=alert_text)
                    pool_alert_last_sent[chat_id] = now
                    logger.info(
                        f"[chat={chat_id}] Sent pool depletion alert: "
                        f"{len(pool_info['warnings'])} warning(s)"
                    )
    except Exception as e:
        logger.warning(f"Pool depletion check failed: {e}")


async def _token_health_tick(
    active_chats: list,
    scheduler_service: SchedulerService,
    health_check_service: HealthCheckService,
    token_alert_last_sent: dict[int, float],
) -> None:
    """Check Google Drive token health and send alerts for expiring tokens."""
    try:
        if active_chats and scheduler_service.telegram_service:
            now_t = time()
            bot = scheduler_service.telegram_service.application.bot
            active_ids = {c.telegram_chat_id for c in active_chats}
            for stale_id in set(token_alert_last_sent) - active_ids:
                del token_alert_last_sent[stale_id]

            for chat in active_chats:
                chat_id = chat.telegram_chat_id
                if (
                    now_t - token_alert_last_sent.get(chat_id, 0)
                    < POOL_ALERT_COOLDOWN_SECONDS
                ):
                    continue

                token_info = health_check_service.check_gdrive_token_for_chat(
                    chat_id, chat_settings=chat
                )
                alert_text = health_check_service.format_token_alert(
                    token_info, chat_id
                )
                if alert_text:
                    await bot.send_message(chat_id=chat_id, text=alert_text)
                    token_alert_last_sent[chat_id] = now_t
                    logger.info(
                        f"[chat={chat_id}] Sent token health alert: "
                        f"{token_info.get('message', '')}"
                    )
    except Exception as e:
        logger.warning(f"Token health check failed: {e}")


async def _token_refresh_tick(
    token_refresh_service: TokenRefreshService,
    scheduler_service: SchedulerService = None,
) -> None:
    """Refresh Instagram tokens that are approaching expiry.

    When refresh fails >= N consecutive times for an account, sends a
    Telegram alert to the admin so they know re-auth is needed.
    """
    try:
        results = await token_refresh_service.refresh_all_instagram_tokens()
        if results["refreshed"] > 0 or results["failed"] > 0:
            logger.info(
                f"Token refresh: {results['refreshed']} refreshed, "
                f"{results['failed']} failed, {results['skipped']} skipped"
            )

        # Send Telegram alerts for accounts with persistent refresh failures
        alerts = results.get("alerts", [])
        if alerts and scheduler_service and scheduler_service.telegram_service:
            from src.config.settings import settings as app_settings

            bot = scheduler_service.telegram_service.application.bot
            admin_chat_id = app_settings.ADMIN_TELEGRAM_CHAT_ID
            for alert in alerts:
                try:
                    await bot.send_message(
                        chat_id=admin_chat_id,
                        text=(f"[Token Alert] {alert['message']}"),
                    )
                    logger.info(
                        f"Sent token refresh alert for account {alert['account']}"
                    )
                except Exception as send_err:
                    logger.warning(f"Failed to send token refresh alert: {send_err}")

    except (TokenRevokedError, TokenCorruptError) as e:
        # Revocation / corruption is not a crash — log clearly
        logger.error(f"Token revoked/corrupt during refresh: {e}")
    except Exception as e:
        logger.warning(f"Token refresh tick failed: {e}")
    finally:
        try:
            token_refresh_service.cleanup_transactions()
        except Exception as cleanup_err:
            logger.warning(
                f"cleanup_transactions failed for TokenRefreshService: {cleanup_err}"
            )


async def _onboarding_cleanup_tick() -> None:
    """Purge expired onboarding / conversation sessions (hourly sub-task)."""
    try:
        from src.services.core.conversation_service import ConversationService

        with ConversationService() as conv_service:
            conv_service.cleanup_expired()
    except Exception as e:
        logger.warning(f"Onboarding session cleanup failed: {e}")


async def _health_checks_tick(
    active_chats: list,
    scheduler_service: SchedulerService,
    health_check_service: HealthCheckService,
    pool_alert_last_sent: dict[int, float],
    token_alert_last_sent: dict[int, float],
) -> None:
    """Run the hourly pool-depletion and Drive-token health checks together."""
    try:
        await asyncio.gather(
            _pool_health_tick(
                active_chats,
                scheduler_service,
                health_check_service,
                pool_alert_last_sent,
            ),
            _token_health_tick(
                active_chats,
                scheduler_service,
                health_check_service,
                token_alert_last_sent,
            ),
        )
    finally:
        try:
            health_check_service.cleanup_transactions()
        except Exception as cleanup_err:
            logger.warning(
                f"cleanup_transactions failed for HealthCheckService: {cleanup_err}"
            )


async def run_scheduler_loop(
    scheduler_service: SchedulerService,
    posting_service: PostingService,
    settings_service=None,
):
    """Run JIT scheduler loop — check for due slots every minute.

    For each active tenant, calls scheduler_service.process_slot() which
    checks is_slot_due() and, if a slot is due, selects media and sends
    to Telegram.  No service_run is created for no-op ticks.

    Also runs periodic sub-tasks (retention cleanup, pool + Drive-token
    health checks, and daily Instagram token refresh) gated on durable
    last-run timestamps so their cadence survives process restarts (#547).

    Args:
        scheduler_service: SchedulerService instance (handles JIT logic)
        posting_service: PostingService instance (handles GDrive alerts)
        settings_service: SettingsService for tenant discovery.
            If None, falls back to global single-tenant behavior.
    """
    logger.info("Starting JIT scheduler loop...")

    queue_repo = QueueRepository()
    service_run_repo = ServiceRunRepository()
    health_check_service = HealthCheckService()
    token_refresh_service = TokenRefreshService()
    # Periodic sub-tasks are gated on durable last-run timestamps rather than
    # in-memory counters, so they survive process restarts (#547).
    periodic = PeriodicScheduler(service_run_repo)
    pool_alert_last_sent: dict[int, float] = {}
    token_alert_last_sent: dict[int, float] = {}
    is_first_tick = True

    while True:
        record_heartbeat("scheduler")

        # --- Scheduler tick: process due slots ---
        active_chats: list = []
        try:
            tick_result = await _scheduler_tick(
                scheduler_service,
                posting_service,
                settings_service,
                queue_repo,
                first_tick=is_first_tick,
            )
            # Only consume the first-tick catch-up on a tick that actually
            # passed the sync gate (returns a list). A sync-wait early return
            # (None) must NOT burn the flag, or the redeploy catch-up never
            # runs in the common sync-enabled deployment (#553).
            if tick_result is not None:
                is_first_tick = False
                active_chats = tick_result
        except Exception as e:
            logger.error(f"Error in scheduler loop: {e}", exc_info=True)
        finally:
            for svc in (scheduler_service, posting_service, settings_service):
                if svc is None:
                    continue
                try:
                    svc.cleanup_transactions()
                except Exception as cleanup_err:
                    logger.warning(
                        f"cleanup_transactions failed for "
                        f"{type(svc).__name__}: {cleanup_err}"
                    )

        # --- Periodic sub-tasks, gated on durable last-run timestamps ---
        # These survive process restarts (unlike the old in-memory tick
        # counters), so token refresh still fires ~daily even when Railway
        # redeploys the worker more often than once per 24h — the acute #547
        # failure where IG tokens silently expired because refresh never ran.
        # A read/write failure here must not kill the loop; log and retry next
        # minute (the per-sub-task functions already swallow their own errors).
        now = datetime.now(timezone.utc)
        try:
            if periodic.due(TASK_RETENTION, RETENTION_INTERVAL_SECONDS, now=now):
                await _retention_cleanup_tick(service_run_repo)
                await _onboarding_cleanup_tick()
                periodic.mark_ran(TASK_RETENTION, now=now)

            if periodic.due(TASK_HEALTH_CHECKS, POOL_CHECK_INTERVAL_SECONDS, now=now):
                await _health_checks_tick(
                    active_chats,
                    scheduler_service,
                    health_check_service,
                    pool_alert_last_sent,
                    token_alert_last_sent,
                )
                periodic.mark_ran(TASK_HEALTH_CHECKS, now=now)

            if periodic.due(
                TASK_TOKEN_REFRESH, TOKEN_REFRESH_INTERVAL_SECONDS, now=now
            ):
                await _token_refresh_tick(token_refresh_service, scheduler_service)
                periodic.mark_ran(TASK_TOKEN_REFRESH, now=now)
        except Exception as e:
            logger.error(f"Periodic sub-task dispatch failed: {e}", exc_info=True)

        await asyncio.sleep(60)
