"""Queue cleanup loop — prunes stale posting_queue rows hourly.

Distinct from the scheduler's ``get_stale_unsent_pending(max_age_minutes=10)``
JIT cleanup which targets short-window scheduler hygiene. This loop catches
the long-tail case where items pile up during an upstream outage and
never get processed (see the 2026-05-17 → 19 burst: 954 items
accumulated over 17 days before manual cleanup). Without this loop a
similar incident would silently bloat the table again.

Button-bearing rows are expired via the shared reap (``expire_sent_row``);
the unstamped accumulation is swept through ``record_expiry_and_delete``
(#687) — see queue_reap.py for why unstamped rows are never raw-deleted.
"""

import asyncio

from src.repositories.history_repository import HistoryRepository
from src.repositories.queue_repository import QueueRepository
from src.services.core.loops.heartbeat import record_heartbeat
from src.services.core.queue_reap import (
    expire_sent_row,
    reconcile_aged_unconfirmed,
    record_expiry_and_delete,
)
from src.utils.logger import logger


async def cleanup_queue_loop(
    queue_repo: QueueRepository,
    bot,
    history_repo: HistoryRepository,
) -> None:
    """Run cleanup loop — reap sent rows, then sweep stale unstamped rows hourly."""
    logger.info("Starting queue cleanup loop...")

    while True:
        record_heartbeat("queue_cleanup")
        try:
            # Gracefully expire button-bearing rows first: their inline
            # buttons are stripped and terminal history written.
            for row in queue_repo.get_stale_sent(hours=24):
                await expire_sent_row(
                    row, bot=bot, history_repo=history_repo, queue_repo=queue_repo
                )

            # Unstamped rows may still have delivered a card (#679/#680), so
            # each deletion writes the terminal 'expired' history row first
            # (#687) — a tap on an orphaned card then shows "Expired"
            # instead of the raw "Queue item not found" error.
            count = 0
            for row in queue_repo.get_stale_unsent(hours=24):
                if record_expiry_and_delete(
                    row, history_repo=history_repo, queue_repo=queue_repo
                ):
                    count += 1
            if count > 0:
                logger.info(f"Cleaned up {count} stale queue items (>24h old)")

            # Aged-reconcile: expire never-tapped 'sent_unconfirmed' rows —
            # the one delivery state no other sweep owns. Bounded + offloaded
            # to a worker thread so its synchronous DB pass can never block the
            # shared event loop (the #682/#573 loop-starvation class). The
            # helper logs its own count.
            await asyncio.to_thread(reconcile_aged_unconfirmed)

        except Exception as e:
            logger.error(f"Error in queue cleanup loop: {e}", exc_info=True)

        # Sleep AFTER the cleanup so a redeploy that SIGKILLs the container
        # during the sleep can never skip a cleanup cycle.
        await asyncio.sleep(3600)
