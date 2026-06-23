"""Queue cleanup loop — prunes stale posting_queue rows hourly.

Distinct from the existing ``delete_stale_pending(max_age_minutes=10)`` JIT
cleanup which targets short-window scheduler hygiene. This loop catches
the long-tail case where items pile up during an upstream outage and
never get processed (see the 2026-05-17 → 19 burst: 954 items
accumulated over 17 days before manual cleanup). Without this loop a
similar incident would silently bloat the table again.
"""

import asyncio

from src.services.core.loops.heartbeat import record_heartbeat
from src.repositories.queue_repository import QueueRepository
from src.utils.logger import logger


async def cleanup_queue_loop(queue_repo: QueueRepository) -> None:
    """Run cleanup loop - remove queue items older than 24h every hour."""
    logger.info("Starting queue cleanup loop...")

    while True:
        record_heartbeat("queue_cleanup")
        try:
            await asyncio.sleep(3600)
            count = queue_repo.delete_stale(hours=24)

            if count > 0:
                logger.info(f"Cleaned up {count} stale queue items (>24h old)")

        except Exception as e:
            logger.error(f"Error in queue cleanup loop: {e}", exc_info=True)
