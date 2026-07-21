"""Operation state management for Telegram callback handlers.

Manages per-queue-item asyncio locks and cancellation flags that prevent
duplicate actions from rapid button clicks and allow terminal actions
(Posted/Skip/Reject) to abort background tasks like auto-posting.
"""

import asyncio


class OperationStateManager:
    """Manages per-queue-item operation locks, cancellation flags, and
    in-flight-autopost markers."""

    def __init__(self):
        self._operation_locks: dict[str, asyncio.Lock] = {}
        self._cancel_flags: dict[str, asyncio.Event] = {}
        self._autopost_inflight: set[str] = set()

    def get_lock(self, queue_id: str) -> asyncio.Lock:
        """Get or create an asyncio lock for a queue item."""
        if queue_id not in self._operation_locks:
            self._operation_locks[queue_id] = asyncio.Lock()
        return self._operation_locks[queue_id]

    def get_cancel_flag(self, queue_id: str) -> asyncio.Event:
        """Get or create a cancellation flag for a queue item."""
        if queue_id not in self._cancel_flags:
            self._cancel_flags[queue_id] = asyncio.Event()
        return self._cancel_flags[queue_id]

    def mark_autopost_inflight(self, queue_id: str) -> None:
        """Mark that an autopost background task is running for a queue item.

        Held from the moment the row is claimed until the task finishes — the
        span the operation lock no longer covers now that it releases before the
        slow edits. A re-tap that sees the marker is rejected, so this is the
        durable guard against a second autopost (and the #549 double-publish).
        """
        self._autopost_inflight.add(queue_id)

    def is_autopost_inflight(self, queue_id: str) -> bool:
        """Whether an autopost background task is currently running for a queue item."""
        return queue_id in self._autopost_inflight

    def cleanup(self, queue_id: str):
        """Remove lock, cancel flag, and in-flight marker after an operation completes."""
        self._operation_locks.pop(queue_id, None)
        self._cancel_flags.pop(queue_id, None)
        self._autopost_inflight.discard(queue_id)
