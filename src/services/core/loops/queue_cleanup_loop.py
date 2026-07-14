"""Queue cleanup loop — prunes stale posting_queue rows hourly.

Distinct from the existing ``delete_stale_pending(max_age_minutes=10)`` JIT
cleanup which targets short-window scheduler hygiene. This loop catches
the long-tail case where items pile up during an upstream outage and
never get processed (see the 2026-05-17 → 19 burst: 954 items
accumulated over 17 days before manual cleanup). Without this loop a
similar incident would silently bloat the table again.

Button-bearing rows (already sent to Telegram) are gracefully expired via
the shared reap (``expire_sent_row``) BEFORE the raw ``delete_stale`` runs,
so their live inline buttons are stripped and a terminal 'expired' history
row is written instead of being orphaned.
"""

import asyncio

from src.repositories.history_repository import HistoryRepository
from src.repositories.queue_repository import QueueRepository
from src.services.core.loops.heartbeat import record_heartbeat
from src.services.core.queue_reap import expire_sent_row
from src.utils.logger import logger


async def cleanup_queue_loop(
    queue_repo: QueueRepository,
    bot,
    history_repo: HistoryRepository,
) -> None:
    """Run cleanup loop — reap sent rows, then delete stale un-sent rows hourly.

    Button-bearing rows (with a ``telegram_message_id``) are expired via the
    shared reap first — buttons stripped, terminal 'expired' history written —
    then ``delete_stale`` removes the remaining never-sent accumulation.
    """
    logger.info("Starting queue cleanup loop...")

    while True:
        record_heartbeat("queue_cleanup")
        try:
            await asyncio.sleep(3600)

            # Gracefully expire button-bearing rows first so their inline
            # buttons are stripped instead of orphaned by delete_stale.
            for row in queue_repo.get_stale_sent(hours=24):
                await expire_sent_row(
                    row, bot=bot, history_repo=history_repo, queue_repo=queue_repo
                )

            count = queue_repo.delete_stale(hours=24)
            if count > 0:
                logger.info(f"Cleaned up {count} stale queue items (>24h old)")

        except Exception as e:
            logger.error(f"Error in queue cleanup loop: {e}", exc_info=True)
