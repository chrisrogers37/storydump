"""Posting queue repository - CRUD operations for posting queue."""

from typing import Optional, List, Iterable
from datetime import datetime, timedelta
from sqlalchemy import and_

from src.repositories.base_repository import BaseRepository
from src.models.posting_queue import PostingQueue
from src.utils.logger import logger


class QueueRepository(BaseRepository):
    """Repository for PostingQueue CRUD operations."""

    def __init__(self):
        super().__init__()

    def get_by_id(
        self, queue_id: str, chat_settings_id: Optional[str] = None
    ) -> Optional[PostingQueue]:
        """Get queue item by ID."""
        result = (
            self._tenant_query(PostingQueue, chat_settings_id)
            .filter(PostingQueue.id == queue_id)
            .first()
        )
        self.end_read_transaction()
        return result

    def claim_for_processing(self, queue_id: str) -> Optional[PostingQueue]:
        """Atomically claim a queue item for callback processing.

        Uses SELECT ... FOR UPDATE SKIP LOCKED so that if two callbacks hit
        the same item concurrently, the second gets None instead of a
        duplicate.

        Items arrive in 'processing' from the scheduler, or 'pending' when a
        callback races the send (e.g. /next force-post). They also arrive in a
        delivery state — 'delivered' (card confirmed in the chat) or
        'sent_unconfirmed' (send dispatched, delivery unconfirmed) — when a human
        taps a card whose row has moved past 'processing'. All are claimable;
        excluding the delivery states would resolve a legitimate tap to None
        ("Queue item not found").

        Returns:
            The claimed PostingQueue item (now in 'processing'), or None if
            already claimed or not found.
        """
        queue_item = (
            self.db.query(PostingQueue)
            .filter(
                PostingQueue.id == queue_id,
                PostingQueue.status.in_(
                    ["pending", "processing", "sent_unconfirmed", "delivered"]
                ),
            )
            .with_for_update(skip_locked=True)
            .first()
        )
        if queue_item is None:
            return None
        queue_item.status = "processing"
        self.db.commit()
        return queue_item

    def get_by_id_prefix(
        self, id_prefix: str, chat_settings_id: Optional[str] = None
    ) -> Optional[PostingQueue]:
        """Get queue item by ID prefix (for shortened callback data).

        Used when Telegram callback data is too long and we need to use
        shortened UUIDs. Returns the first matching item.

        Args:
            id_prefix: First N characters of a UUID (typically 8)
            chat_settings_id: Optional tenant filter

        Returns:
            PostingQueue item or None if not found
        """
        from sqlalchemy import cast, String

        result = (
            self._tenant_query(PostingQueue, chat_settings_id)
            .filter(cast(PostingQueue.id, String).like(f"{id_prefix}%"))
            .first()
        )
        self.end_read_transaction()
        return result

    def get_by_media_id(
        self, media_id: str, chat_settings_id: Optional[str] = None
    ) -> Optional[PostingQueue]:
        """Get queue item by media ID."""
        result = (
            self._tenant_query(PostingQueue, chat_settings_id)
            .filter(PostingQueue.media_item_id == media_id)
            .first()
        )
        self.end_read_transaction()
        return result

    def get_pending(
        self, limit: Optional[int] = None, chat_settings_id: Optional[str] = None
    ) -> List[PostingQueue]:
        """Get pending queue items ready to process.

        Uses FOR UPDATE SKIP LOCKED to prevent concurrent scheduler
        instances from claiming the same rows.
        """
        now = datetime.utcnow()
        query = self._tenant_query(PostingQueue, chat_settings_id).filter(
            and_(PostingQueue.status == "pending", PostingQueue.scheduled_for <= now)
        )
        query = query.order_by(PostingQueue.scheduled_for.asc())

        if limit:
            query = query.limit(limit)

        query = query.with_for_update(skip_locked=True)

        return query.all()

    def get_all(
        self, status: Optional[str] = None, chat_settings_id: Optional[str] = None
    ) -> List[PostingQueue]:
        """Get all queue items, optionally filtered by status."""
        query = self._tenant_query(PostingQueue, chat_settings_id)

        if status:
            query = query.filter(PostingQueue.status == status)

        result = query.order_by(PostingQueue.scheduled_for.asc()).all()
        self.end_read_transaction()
        return result

    def get_all_with_media(
        self,
        status: Optional[str] = None,
        chat_settings_id: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list:
        """Get queue items with joined media info (file_name, category).

        Returns list of (PostingQueue, file_name, category) tuples.
        """
        from src.models.media_item import MediaItem

        query = (
            self._tenant_query(PostingQueue, chat_settings_id)
            .outerjoin(MediaItem, PostingQueue.media_item_id == MediaItem.id)
            .add_columns(MediaItem.file_name, MediaItem.category)
        )

        if status:
            query = query.filter(PostingQueue.status == status)

        query = query.order_by(PostingQueue.scheduled_for.asc())
        if limit is not None:
            query = query.limit(limit)
        result = query.all()
        self.end_read_transaction()
        return result

    def count_by_status(
        self,
        statuses: list[str],
        chat_settings_id: Optional[str] = None,
    ) -> int:
        """Count items matching any of the given statuses."""
        result = (
            self._tenant_query(PostingQueue, chat_settings_id)
            .filter(PostingQueue.status.in_(statuses))
            .count()
        )
        self.end_read_transaction()
        return result

    def count_recent_by_status(
        self,
        statuses: list[str],
        since: datetime,
        chat_settings_id: Optional[str] = None,
    ) -> int:
        """Count items matching any status whose ``created_at`` is at or after
        ``since``.

        Time-bounded sibling of ``count_by_status`` for the daily-cap gate: a
        'publishing' row only taxes the cap while it is plausibly a live,
        in-flight publish. A publish resolves within the 180s container
        wall-clock cap, so a row older than the caller's ``since`` threshold is
        presumed stuck and excluded — a stranded row must not wedge the cap
        forever. This only stops *counting* a stale claim; it never deletes or
        rolls the row forward (no reconciliation).
        """
        result = (
            self._tenant_query(PostingQueue, chat_settings_id)
            .filter(PostingQueue.status.in_(statuses))
            .filter(PostingQueue.created_at >= since)
            .count()
        )
        self.end_read_transaction()
        return result

    def count_pending(self, chat_settings_id: Optional[str] = None) -> int:
        """Count number of pending items."""
        result = (
            self._tenant_query(PostingQueue, chat_settings_id)
            .filter(PostingQueue.status == "pending")
            .count()
        )
        self.end_read_transaction()
        return result

    def get_oldest_pending(
        self, chat_settings_id: Optional[str] = None
    ) -> Optional[PostingQueue]:
        """Get the oldest pending item."""
        result = (
            self._tenant_query(PostingQueue, chat_settings_id)
            .filter(PostingQueue.status == "pending")
            .order_by(PostingQueue.created_at.asc())
            .first()
        )
        self.end_read_transaction()
        return result

    def create(
        self,
        media_item_id: str,
        scheduled_for: datetime,
        chat_settings_id: Optional[str] = None,
    ) -> PostingQueue:
        """Create a new queue item."""
        queue_item = PostingQueue(
            media_item_id=media_item_id,
            scheduled_for=scheduled_for,
            chat_settings_id=chat_settings_id,
        )
        self.db.add(queue_item)
        self.db.commit()
        self.db.refresh(queue_item)
        return queue_item

    def transition(
        self,
        queue_id: str,
        to_status: str,
        allowed_from: Optional[Iterable[str]] = None,
    ) -> Optional[PostingQueue]:
        """Move a queue row to ``to_status`` through one atomic guarded seam.

        The single place delivery-state writes flow through. The ``allowed_from``
        guard is enforced in the UPDATE's own WHERE clause — a single conditional
        statement, not a read-then-write — so two concurrent transitions out of
        the same state can never both succeed: the first flips the row out of
        ``allowed_from`` and the rest match zero rows and return ``None`` (the
        row moved under them — a concurrent claim or reap). This mirrors
        ``claim_for_processing``'s concurrency-safe conditional claim. With
        ``allowed_from=None`` the status is set unconditionally.

        Returns the updated row, or ``None`` if the row is gone or its current
        status is outside ``allowed_from``.
        """
        query = self.db.query(PostingQueue).filter(PostingQueue.id == queue_id)
        if allowed_from is not None:
            query = query.filter(PostingQueue.status.in_(list(allowed_from)))
        # synchronize_session=False keeps this ONE statement. "fetch" may
        # split it into a pre-SELECT + primary-key UPDATE, and that second
        # statement carries no allowed_from guard — a transition blocked on a
        # concurrent lock then re-applies to a row that already left the
        # allowed set (the read-check-write TOCTOU this method exists to
        # close; surfaced as a check_delivered_stamped violation under the
        # concurrency suite). Session state is refreshed by the get_by_id
        # re-read below, so no sync strategy is needed.
        updated = query.update(
            {PostingQueue.status: to_status}, synchronize_session=False
        )
        self.db.commit()
        if updated == 0:
            allowed = set(allowed_from) if allowed_from is not None else "any"
            logger.info(
                f"Queue item {queue_id} not transitioned to {to_status} "
                f"(missing, or current status not in {allowed})"
            )
            return None
        return self.get_by_id(queue_id)

    def update_status(self, queue_id: str, status: str) -> Optional[PostingQueue]:
        """Update queue item status unconditionally (see :meth:`transition` for
        a from-state-guarded write)."""
        return self.transition(queue_id, status)

    def mark_publishing(self, queue_id: str, container_id: str) -> PostingQueue:
        """Claim a queue row for an in-flight Instagram publish (#549).

        Flips the row to ``status='publishing'`` and persists the IG media
        ``container_id`` — done the instant the container is created and BEFORE
        the publish call. A 'publishing' row is excluded from every stale-sweep
        and blocks reselection (it is still "already queued"), so a crash after
        the publish leaves a recoverable, non-duplicating row rather than
        re-serving the media.
        """
        queue_item = self.get_by_id(queue_id)
        if queue_item:
            queue_item.status = "publishing"
            queue_item.instagram_container_id = container_id
            self.db.commit()
            self.db.refresh(queue_item)
        return queue_item

    def update_scheduled_time(
        self, queue_id: str, scheduled_for: datetime
    ) -> PostingQueue:
        """Update queue item scheduled time."""
        queue_item = self.get_by_id(queue_id)
        if queue_item:
            queue_item.scheduled_for = scheduled_for
            self.db.commit()
            self.db.refresh(queue_item)
        return queue_item

    def set_telegram_message(
        self, queue_id: str, message_id: int, chat_id: int
    ) -> PostingQueue:
        """Stamp the row with its Telegram card and promote it to 'delivered'.

        INV-1: ``delivered`` ⟺ stamped — a landed stamp IS delivery
        confirmation, whether it arrives from the send path or from the
        callback-time stamp heal (a recovered stamp resolves
        ``sent_unconfirmed`` one-way to ``delivered``). Guarded: a
        ``publishing`` row keeps its status (the IG claim anchor stays
        authoritative, #549), as does terminal ``failed``.
        """
        queue_item = self.get_by_id(queue_id)
        if queue_item:
            queue_item.telegram_message_id = message_id
            queue_item.telegram_chat_id = chat_id
            if queue_item.status in ("pending", "processing", "sent_unconfirmed"):
                queue_item.status = "delivered"
            self.db.commit()
            self.db.refresh(queue_item)
        return queue_item

    def delete(self, queue_id: str) -> bool:
        """Delete a queue item (after moving to history)."""
        queue_item = self.get_by_id(queue_id)
        if queue_item:
            self.db.delete(queue_item)
            self.db.commit()
            return True
        return False

    def _get_stale_scheduled(
        self, *, stamped: bool, hours: int, status: Optional[str] = None
    ) -> List[PostingQueue]:
        """Age-based fetch of rows past their reap age, split by stamp polarity.

        Backs ``get_stale_sent`` / ``get_stale_unsent`` so the reap-age query
        — and its 'publishing' guard — cannot drift between the two.
        Ordered by ``scheduled_for`` ascending (oldest first).
        """
        cutoff = datetime.utcnow() - timedelta(hours=hours)
        msg_id = PostingQueue.telegram_message_id
        query = self.db.query(PostingQueue).filter(
            msg_id.isnot(None) if stamped else msg_id.is_(None),
            PostingQueue.scheduled_for < cutoff,
            # Never reap a claimed-but-unconfirmed publish (#549): whether
            # stamped (stuck autopost card) or not (auto-approve path), a
            # 'publishing' row must persist to block reselection of a
            # maybe-posted story.
            PostingQueue.status != "publishing",
            # 'sent_unconfirmed' is owned by the aged-reconcile lifecycle
            # (promote on click / expire when aged), not this generic sweep:
            # reaping it here would race that purpose-built resolution.
            # 'delivered' stays sweepable on the stamped side — an unacted
            # card must still age out (the stranding safety net).
            PostingQueue.status != "sent_unconfirmed",
        )
        if status:
            query = query.filter(PostingQueue.status == status)
        result = query.order_by(PostingQueue.scheduled_for.asc()).all()
        self.end_read_transaction()
        return result

    def get_stale_unsent(self, hours: int = 24) -> List[PostingQueue]:
        """Get unstamped queue rows whose scheduled_for is older than ``hours``.

        Feeds the hourly ``cleanup_queue_loop`` sweep of the long-tail
        accumulation from an upstream outage (the 2026-05-17 → 19 burst left
        954 such rows). An unstamped row may still have delivered a card, so
        the caller must delete through ``record_expiry_and_delete`` (#687),
        never a raw hard-delete.

        Args:
            hours: Age threshold in hours (default: 24).

        Returns:
            List of unstamped PostingQueue items older than the cutoff.
        """
        return self._get_stale_scheduled(stamped=False, hours=hours)

    def get_stale_unsent_pending(self, max_age_minutes: int = 10) -> List[PostingQueue]:
        """Get unstamped pending items older than ``max_age_minutes``.

        Feeds the scheduler's defense-in-depth sweep of items orphaned by
        crashes or restarts. Normal failures delete the queue item
        immediately in _send_to_telegram(); this catches anything that
        slipped through. The caller must delete through
        ``record_expiry_and_delete`` (#687), never a raw hard-delete.

        Ordered by ``created_at`` ascending (oldest first).

        Args:
            max_age_minutes: Minutes after which an unsent pending item
                is considered stale (default: 10).

        Returns:
            List of unstamped pending PostingQueue items older than the cutoff.
        """
        cutoff = datetime.utcnow() - timedelta(minutes=max_age_minutes)
        stale = (
            self.db.query(PostingQueue)
            .filter(
                PostingQueue.status == "pending",
                PostingQueue.telegram_message_id.is_(None),
                PostingQueue.created_at <= cutoff,
            )
            .order_by(PostingQueue.created_at.asc())
            .all()
        )
        self.end_read_transaction()
        return stale

    def resolve_stale_processing(self, max_age_minutes: int = 10) -> int:
        """Resolve rows stuck in 'processing' to their delivery state.

        Selection keys on status + age ONLY (INV-2): a NULL
        ``telegram_message_id`` is not proof the send never happened
        (#679/#680), so NULL-ness never decides whether a row is swept.
        Disposition applies INV-1's definition:

        - stamped → ``delivered`` — the card is confirmed in the chat; the
          row just missed its promote (e.g. a crash between stamp and
          transition).
        - unstamped → ``sent_unconfirmed`` — outcome unknown; parking is
          fail-safe because nothing is ever re-sent.

        Nothing is ever reset to 'pending': requeueing re-arms the send path,
        which is exactly the #680 double-card mechanism. Parked rows remain
        claimable on tap (``claim_for_processing`` accepts the delivery
        states); their aged expiry belongs to the reconcile sweep, not this
        resolver.

        Args:
            max_age_minutes: Minutes after which a processing item is
                considered stuck (default: 10).

        Returns:
            Number of items resolved.
        """
        cutoff = datetime.utcnow() - timedelta(minutes=max_age_minutes)
        stale = (
            self.db.query(PostingQueue)
            .filter(
                PostingQueue.status == "processing",
                PostingQueue.created_at <= cutoff,
            )
            .all()
        )

        for item in stale:
            resolved_to = (
                "delivered"
                if item.telegram_message_id is not None
                else "sent_unconfirmed"
            )
            logger.info(
                f"Resolving stale processing item {item.id} -> {resolved_to} "
                f"(age={datetime.utcnow() - item.created_at})"
            )
            item.status = resolved_to

        if stale:
            self.db.commit()

        return len(stale)

    def get_stale_sent(
        self, hours: int = 24, status: Optional[str] = None
    ) -> List[PostingQueue]:
        """Get button-bearing queue rows past their reap age.

        Returns rows that carry a ``telegram_message_id`` — a live Telegram
        card with inline buttons — whose ``scheduled_for`` is more than
        ``hours`` ago. These must be handled by the shared reap
        (``expire_sent_row``), which strips the buttons and writes a terminal
        history row, rather than being hard-deleted and orphaning the card.

        Ordered by ``scheduled_for`` ascending (oldest first).

        Args:
            hours: Age threshold in hours (default: 24).
            status: Optional status filter (e.g. 'processing').

        Returns:
            List of button-bearing PostingQueue items older than the cutoff.
        """
        return self._get_stale_scheduled(stamped=True, hours=hours, status=status)

    def get_aged_sent_unconfirmed(
        self, hours: int = 24, limit: int = 100
    ) -> List[PostingQueue]:
        """Get 'sent_unconfirmed' rows past their reconcile age, oldest first.

        The aged-reconcile lifecycle owns 'sent_unconfirmed': a tap promotes
        the row to 'delivered' (``set_telegram_message``); this feed ages out
        the never-tapped ones to terminal 'expired' via
        ``record_expiry_and_delete`` (#687 — history-first, never a re-send).
        These rows are deliberately excluded from ``_get_stale_scheduled`` so
        the generic hourly sweep can't race this purpose-built resolution —
        this method is their only reaper.

        Selection keys on status + ``scheduled_for`` age only (INV-2), the same
        age predicate as the sibling reap sweeps. Bounded by ``limit`` so one
        pass can never block the event loop on a large backlog (the #682
        loop-starvation class) — the hourly cadence drains the remainder.

        Args:
            hours: Age threshold in hours (default: 24).
            limit: Maximum rows returned per pass (default: 100).

        Returns:
            Up to ``limit`` sent_unconfirmed PostingQueue rows older than the
            cutoff, ordered by ``scheduled_for`` ascending (oldest first).
        """
        cutoff = datetime.utcnow() - timedelta(hours=hours)
        rows = (
            self.db.query(PostingQueue)
            .filter(
                PostingQueue.status == "sent_unconfirmed",
                PostingQueue.scheduled_for < cutoff,
            )
            .order_by(PostingQueue.scheduled_for.asc())
            .limit(limit)
            .all()
        )
        self.end_read_transaction()
        return rows

    def get_pending_with_telegram_message(
        self, telegram_chat_id: int
    ) -> List[PostingQueue]:
        """Get active queue items whose card is live in a Telegram chat.

        Used to find all active notifications for a chat so their captions
        and keyboards can be batch-updated (e.g., after an account switch).
        Covers 'delivered' (the stamped resting state under INV-1) alongside
        the transient pending/processing shapes.

        Args:
            telegram_chat_id: The Telegram chat ID to filter by

        Returns:
            List of PostingQueue items with telegram_message_id set
        """
        result = (
            self.db.query(PostingQueue)
            .filter(
                PostingQueue.telegram_chat_id == telegram_chat_id,
                PostingQueue.telegram_message_id.isnot(None),
                PostingQueue.status.in_(["pending", "processing", "delivered"]),
            )
            .order_by(PostingQueue.scheduled_for.asc())
            .all()
        )
        self.end_read_transaction()
        return result

    def delete_all_pending(self, chat_settings_id: Optional[str] = None) -> int:
        """Delete all pending queue items. Returns count of deleted items."""
        count = (
            self._tenant_query(PostingQueue, chat_settings_id)
            .filter(PostingQueue.status == "pending")
            .delete()
        )
        self.db.commit()
        return count
