"""Main application entry point - runs scheduler + Telegram bot."""

import asyncio
import json
import os
import signal
import sys
from time import time
from typing import Optional

from src.services.core.loops.guarded import guarded
from src.services.core.loops.heartbeat import get_loop_liveness
from src.services.core.loops.lifecycle import (
    log_service_summary,
    session_state,
    validate_and_log_startup,
)
from src.services.core.loops.scheduler_loop import run_scheduler_loop
from src.services.core.loops.lock_cleanup_loop import cleanup_locks_loop
from src.services.core.loops.cloud_cleanup_loop import cleanup_cloud_storage_loop
from src.services.core.loops.transaction_cleanup_loop import transaction_cleanup_loop
from src.services.core.loops.media_sync_loop import media_sync_loop
from src.utils.logger import logger

STARTUP_GRACE_SECONDS = 120


def _build_health_response() -> bytes:
    """Build an HTTP response based on loop liveness.

    Returns 200 during the startup grace period (loops haven't ticked yet),
    200 if all loops are alive, 503 with stale loop details otherwise.
    """
    # During startup, loops haven't ticked yet — always report healthy.
    start = session_state.start_time
    if start and (time() - start) < STARTUP_GRACE_SECONDS:
        body = json.dumps({"status": "healthy", "grace_period": True})
        body_bytes = body.encode()
        return (
            f"HTTP/1.1 200 OK\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body_bytes)}\r\n"
            f"\r\n"
        ).encode() + body_bytes

    liveness = get_loop_liveness()
    stale = {name: info for name, info in liveness.items() if not info["alive"]}

    if stale:
        body = json.dumps({"status": "unhealthy", "stale_loops": stale})
        status_line = "HTTP/1.1 503 Service Unavailable"
    else:
        body = json.dumps({"status": "healthy"})
        status_line = "HTTP/1.1 200 OK"

    body_bytes = body.encode()
    return (
        f"{status_line}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body_bytes)}\r\n"
        f"\r\n"
    ).encode() + body_bytes


async def _health_check_server():
    """Minimal HTTP server for Railway's health check.

    Returns 200 when all background loops are alive, 503 with details
    about which loops are stale when any loop has missed its heartbeat
    window (>2x expected interval).
    """
    port = int(os.environ.get("PORT", 8080))

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            await reader.readline()  # consume request line
            writer.write(_build_health_response())
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "0.0.0.0", port)
    logger.info(f"Health check server listening on port {port}")
    async with server:
        await server.serve_forever()


async def main_async():
    """Main async application entry point."""
    validate_and_log_startup()

    # Start the health check server IMMEDIATELY, before any awaits that could
    # delay Railway's healthcheck. Originally the server was started inside
    # the task list at the bottom of this function (behind telegram_service
    # init + send_startup_notification), which meant any slowness in those
    # steps would cause Railway to time out on /health and mark the deploy
    # FAILED — even though Python was fine. Start now, record the start
    # time so the 120s startup grace in `_build_health_response` activates.
    session_state.start_time = time()
    health_task = asyncio.create_task(_health_check_server())

    # Initialize services
    from src.services.core.settings_service import SettingsService
    from src.services.core.posting import PostingService
    from src.services.core.scheduler import SchedulerService
    from src.services.core.telegram_service import TelegramService
    from src.services.core.media_lock import MediaLockService
    from src.services.core.media_sync import MediaSyncService

    scheduler_service = SchedulerService()
    posting_service = PostingService()
    telegram_service = TelegramService()
    lock_service = MediaLockService()
    settings_service = SettingsService()

    # Media sync runs unconditionally — the loop iterates per-chat and skips
    # chats with media_sync_enabled=False. No env gate.
    sync_service = MediaSyncService()

    await telegram_service.initialize()
    scheduler_service.telegram_service = telegram_service

    # Send startup notification (health server is already up, so a hang
    # here no longer fails Railway's healthcheck).
    await telegram_service.send_startup_notification()

    # Create tasks
    all_services = [
        scheduler_service,
        posting_service,
        telegram_service,
        lock_service,
        settings_service,
    ]
    bot = telegram_service.bot

    tasks = [
        # health_task was started above and is included here so it gets
        # cancelled cleanly during shutdown along with the rest.
        health_task,
        asyncio.create_task(
            guarded(
                "scheduler",
                lambda: run_scheduler_loop(
                    scheduler_service, posting_service, settings_service
                ),
                bot=bot,
            )
        ),
        asyncio.create_task(
            guarded("lock_cleanup", lambda: cleanup_locks_loop(lock_service), bot=bot)
        ),
        asyncio.create_task(telegram_service.start_polling()),
    ]

    # Add cloud storage cleanup loop if Cloudinary is configured
    from src.services.integrations.cloud_storage import CloudStorageService

    cloud_service = CloudStorageService()
    if cloud_service.is_configured():
        all_services.append(cloud_service)
        tasks.append(
            asyncio.create_task(
                guarded(
                    "cloud_cleanup",
                    lambda: cleanup_cloud_storage_loop(cloud_service),
                    bot=bot,
                )
            )
        )

    # Add media sync loop if enabled
    if sync_service:
        all_services.append(sync_service)
        tasks.append(
            asyncio.create_task(
                guarded(
                    "media_sync",
                    lambda: media_sync_loop(
                        sync_service,
                        settings_service=settings_service,
                        telegram_service=telegram_service,
                    ),
                    bot=bot,
                )
            )
        )
    else:
        # No sync loop — open the scheduler gate immediately
        session_state.initial_sync_complete = True

    tasks.append(
        asyncio.create_task(
            guarded(
                "transaction_cleanup",
                lambda: transaction_cleanup_loop(all_services),
                bot=bot,
            )
        )
    )

    log_service_summary()

    # Setup signal handlers for graceful shutdown.
    #
    # The handler is wrapped in a tracked task so the main coroutine can
    # explicitly await its completion below \u2014 otherwise asyncio.create_task
    # is fire-and-forget and the process can exit before stop_polling()
    # finishes telling Telegram the session is over. That's exactly what
    # caused new worker deploys to fail with
    # `telegram.error.Conflict: terminated by other getUpdates request` \u2014 the
    # old worker's Python process died before Telegram noticed the session
    # was released. See #392.
    shutdown_task: Optional[asyncio.Task] = None

    async def shutdown_handler(sig):
        """Handle shutdown signals gracefully."""
        # Guard against duplicate signals
        if session_state.shutdown_in_progress:
            logger.info(f"Shutdown already in progress, ignoring {sig.name} signal")
            return
        session_state.shutdown_in_progress = True

        logger.info(f"Received {sig.name} signal...")

        # Calculate uptime
        uptime = (
            int(time() - session_state.start_time) if session_state.start_time else 0
        )

        # Send shutdown notification
        try:
            await telegram_service.send_shutdown_notification(
                uptime_seconds=uptime, posts_sent=session_state.posts_sent
            )
        except Exception as e:
            logger.warning(f"Failed to send shutdown notification: {e}")

        # Stop Telegram polling FIRST. This is the critical step: it calls
        # application.updater.stop() \u2192 application.stop() \u2192
        # application.shutdown(), which closes the long-poll HTTP connection
        # cleanly so Telegram releases the session for the next worker.
        try:
            await telegram_service.stop_polling()
        except Exception as e:
            logger.warning(f"Error stopping Telegram polling: {e}")

        # Now cancel the background tasks (scheduler loop, etc.)
        for task in tasks:
            task.cancel()

        logger.info("\u2713 Shutdown complete")

    def _on_signal(sig):
        """Synchronous signal callback \u2014 schedules the async handler exactly once."""
        nonlocal shutdown_task
        if shutdown_task is None:
            shutdown_task = asyncio.create_task(shutdown_handler(sig))

    # Register signal handlers
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _on_signal, sig)

    # Wait for all tasks
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        # Tasks were cancelled during shutdown
        pass
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt...")
        await shutdown_handler(signal.SIGINT)

    # Block until the shutdown handler has fully released Telegram and run
    # cleanup. Without this await, the process can exit while stop_polling
    # is still mid-call \u2014 Telegram never sees the session close and the
    # next deploy hits a polling-conflict on startup.
    if shutdown_task is not None:
        try:
            await shutdown_task
        except Exception as e:
            logger.warning(f"Shutdown handler raised: {e}")


def main():
    """Main entry point."""
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        logger.info("Application stopped by user")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
