"""Integration tests: status-only sweeps + the delivered-on-stamp seam (INV-1/INV-2).

The delivery-state machine (#684, PR3 of the data-model redesign) changes how
stale ``posting_queue`` rows are dispositioned:

* ``resolve_stale_processing`` replaces ``requeue_stale_processing``. Selection
  keys on **status + age only** (INV-2 — no ``telegram_message_id IS NULL``
  lifecycle inference); disposition applies INV-1's definitional split:
  stamped → ``delivered``, unstamped → ``sent_unconfirmed`` (fail-safe — a
  maybe-delivered card is never reset to 'pending' and re-sent, the #680 class).

* ``set_telegram_message`` promotes the row to ``delivered`` when the stamp
  lands (INV-1: delivered ⟺ stamped), guarded so it never clobbers the
  ``publishing``/``failed`` states.

* ``_get_stale_scheduled`` (the hourly ``cleanup_queue_loop`` feed) excludes
  ``sent_unconfirmed`` — its lifecycle belongs to the aged reconcile (PR4),
  not the generic 24h sweep — while ``delivered`` rows REMAIN sweepable on the
  stamped side (the stranding safety net).

Real DB: repos are routed at the current-schema ``.env.test`` database (same
pattern as test_queue_claim_concurrency); every row is deleted in ``finally``.
"""

from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.orm import sessionmaker

from src.repositories.media_repository import MediaRepository
from src.repositories.queue_repository import QueueRepository

STALE_MINUTES = 10


@pytest.fixture(autouse=True)
def _route_repos_to_test_db(setup_test_database, monkeypatch):
    """Route the production repo session factory at the current-schema test DB
    (see test_queue_claim_concurrency for the full rationale)."""
    if setup_test_database is None:
        pytest.skip("Database not available - skipping integration test")

    import src.config.database as db_module

    monkeypatch.setattr(
        db_module,
        "SessionLocal",
        sessionmaker(
            autocommit=False,
            autoflush=False,
            bind=setup_test_database,
            expire_on_commit=False,
        ),
    )
    yield


def _create_queue_row(
    *,
    status: str,
    stamped: bool = False,
    age_minutes: int = 0,
    scheduled_age_hours: float = 0,
) -> tuple:
    """Create a media_item + posting_queue row in the given lifecycle shape.

    Returns ``(media_id, queue_id)``; caller MUST clean up via ``_delete_rows``.
    ``age_minutes`` backdates ``created_at`` (the resolve-sweep predicate);
    ``scheduled_age_hours`` backdates ``scheduled_for`` (the reap predicate).
    """
    media_repo = MediaRepository()
    try:
        media = media_repo.create(
            file_path=f"/test/queue-sweep-status/{uuid4()}.jpg",
            file_name="sweep_status.jpg",
            file_hash=uuid4().hex,
            file_size_bytes=1024,
            mime_type="image/jpeg",
        )
        media_id = media.id
    finally:
        media_repo.close()

    queue_repo = QueueRepository()
    try:
        item = queue_repo.create(
            media_item_id=media_id,
            scheduled_for=datetime.utcnow() - timedelta(hours=scheduled_age_hours),
        )
        queue_id = item.id
        row = queue_repo.db.query(type(item)).filter(type(item).id == queue_id).one()
        row.status = status
        if stamped:
            row.telegram_message_id = 424242
            row.telegram_chat_id = 111111
        if age_minutes:
            row.created_at = datetime.utcnow() - timedelta(minutes=age_minutes)
        queue_repo.db.commit()
    finally:
        queue_repo.close()

    return media_id, queue_id


def _delete_rows(*pairs) -> None:
    for media_id, queue_id in pairs:
        queue_repo = QueueRepository()
        try:
            queue_repo.delete(str(queue_id))
        finally:
            queue_repo.close()
        media_repo = MediaRepository()
        try:
            media_repo.delete(str(media_id))
        finally:
            media_repo.close()


def _status_of(queue_id) -> str:
    repo = QueueRepository()
    try:
        return repo.get_by_id(str(queue_id)).status
    finally:
        repo.close()


@pytest.mark.integration
class TestResolveStaleProcessing:
    """INV-2 selection (status + age only) with INV-1 disposition."""

    def test_stale_stamped_processing_promotes_to_delivered(self):
        pair = _create_queue_row(
            status="processing", stamped=True, age_minutes=STALE_MINUTES + 5
        )
        try:
            repo = QueueRepository()
            try:
                resolved = repo.resolve_stale_processing(max_age_minutes=STALE_MINUTES)
            finally:
                repo.close()
            assert resolved == 1
            assert _status_of(pair[1]) == "delivered"
        finally:
            _delete_rows(pair)

    def test_stale_unstamped_processing_parks_as_sent_unconfirmed(self):
        """Fail-safe: NULL proves nothing (#679/#680) — park, never requeue."""
        pair = _create_queue_row(
            status="processing", stamped=False, age_minutes=STALE_MINUTES + 5
        )
        try:
            repo = QueueRepository()
            try:
                resolved = repo.resolve_stale_processing(max_age_minutes=STALE_MINUTES)
            finally:
                repo.close()
            assert resolved == 1
            assert _status_of(pair[1]) == "sent_unconfirmed"
        finally:
            _delete_rows(pair)

    def test_fresh_processing_rows_are_untouched(self):
        pair = _create_queue_row(status="processing", stamped=False, age_minutes=1)
        try:
            repo = QueueRepository()
            try:
                resolved = repo.resolve_stale_processing(max_age_minutes=STALE_MINUTES)
            finally:
                repo.close()
            assert resolved == 0
            assert _status_of(pair[1]) == "processing"
        finally:
            _delete_rows(pair)

    def test_selection_is_status_scoped(self):
        """publishing / pending / sent_unconfirmed rows are outside the sweep
        no matter their age or stamp shape (INV-2: status-only selection)."""
        pairs = [
            _create_queue_row(
                status="publishing", stamped=True, age_minutes=STALE_MINUTES + 60
            ),
            _create_queue_row(
                status="pending", stamped=False, age_minutes=STALE_MINUTES + 60
            ),
            _create_queue_row(
                status="sent_unconfirmed",
                stamped=False,
                age_minutes=STALE_MINUTES + 60,
            ),
        ]
        try:
            repo = QueueRepository()
            try:
                resolved = repo.resolve_stale_processing(max_age_minutes=STALE_MINUTES)
            finally:
                repo.close()
            assert resolved == 0
            assert _status_of(pairs[0][1]) == "publishing"
            assert _status_of(pairs[1][1]) == "pending"
            assert _status_of(pairs[2][1]) == "sent_unconfirmed"
        finally:
            _delete_rows(*pairs)

    def test_nothing_is_ever_requeued_to_pending(self):
        """The #680 kill: no disposition of a stale processing row may re-arm
        the send path. Whatever the stamp shape, the row must never come out of
        the sweep as 'pending'."""
        pairs = [
            _create_queue_row(
                status="processing", stamped=True, age_minutes=STALE_MINUTES + 5
            ),
            _create_queue_row(
                status="processing", stamped=False, age_minutes=STALE_MINUTES + 5
            ),
        ]
        try:
            repo = QueueRepository()
            try:
                repo.resolve_stale_processing(max_age_minutes=STALE_MINUTES)
            finally:
                repo.close()
            assert _status_of(pairs[0][1]) != "pending"
            assert _status_of(pairs[1][1]) != "pending"
        finally:
            _delete_rows(*pairs)

    def test_second_run_is_a_no_op(self):
        pair = _create_queue_row(
            status="processing", stamped=True, age_minutes=STALE_MINUTES + 5
        )
        try:
            repo = QueueRepository()
            try:
                first = repo.resolve_stale_processing(max_age_minutes=STALE_MINUTES)
                second = repo.resolve_stale_processing(max_age_minutes=STALE_MINUTES)
            finally:
                repo.close()
            assert (first, second) == (1, 0)
            assert _status_of(pair[1]) == "delivered"
        finally:
            _delete_rows(pair)


@pytest.mark.integration
class TestDeliveredOnStamp:
    """INV-1 at the seam: stamping a row IS confirming delivery."""

    def test_stamp_promotes_processing_to_delivered(self):
        pair = _create_queue_row(status="processing")
        try:
            repo = QueueRepository()
            try:
                repo.set_telegram_message(str(pair[1]), 555001, 222333)
            finally:
                repo.close()
            assert _status_of(pair[1]) == "delivered"
        finally:
            _delete_rows(pair)

    def test_stamp_promotes_sent_unconfirmed_to_delivered(self):
        """A recovered stamp resolves the ambiguity one-way (enum contract)."""
        pair = _create_queue_row(status="sent_unconfirmed")
        try:
            repo = QueueRepository()
            try:
                repo.set_telegram_message(str(pair[1]), 555002, 222333)
            finally:
                repo.close()
            assert _status_of(pair[1]) == "delivered"
        finally:
            _delete_rows(pair)

    def test_stamp_never_clobbers_publishing(self):
        """A 'publishing' row (IG claim anchor, #549) must keep its status —
        the stamp records the card, the publish claim stays authoritative."""
        pair = _create_queue_row(status="publishing")
        try:
            repo = QueueRepository()
            try:
                repo.set_telegram_message(str(pair[1]), 555003, 222333)
            finally:
                repo.close()
            row_repo = QueueRepository()
            try:
                row = row_repo.get_by_id(str(pair[1]))
                assert row.status == "publishing"
                assert row.telegram_message_id == 555003
            finally:
                row_repo.close()
        finally:
            _delete_rows(pair)

    def test_delivered_rows_included_in_batch_card_updates(self):
        """Account-switch batch updates (get_pending_with_telegram_message)
        must keep covering cards after their rows promote to delivered."""
        pair = _create_queue_row(status="delivered", stamped=True)
        try:
            repo = QueueRepository()
            try:
                rows = repo.get_pending_with_telegram_message(111111)
            finally:
                repo.close()
            assert str(pair[1]) in {str(r.id) for r in rows}
        finally:
            _delete_rows(pair)


@pytest.mark.integration
class TestStaleScheduledStatusAware:
    """M3: the hourly sweep must not eat sent_unconfirmed; delivered stays
    covered (the stranding safety net); processing+NULL@24h coverage holds
    (what makes discard_abandoned_processing safely deletable)."""

    def test_hourly_unsent_sweep_excludes_sent_unconfirmed(self):
        pair = _create_queue_row(
            status="sent_unconfirmed", stamped=False, scheduled_age_hours=48
        )
        try:
            repo = QueueRepository()
            try:
                stale = repo.get_stale_unsent(hours=24)
            finally:
                repo.close()
            assert str(pair[1]) not in {str(r.id) for r in stale}
        finally:
            _delete_rows(pair)

    def test_stamped_sweep_excludes_sent_unconfirmed(self):
        """Exclusion holds on both stamp polarities of the shared helper."""
        pair = _create_queue_row(
            status="sent_unconfirmed", stamped=True, scheduled_age_hours=48
        )
        try:
            repo = QueueRepository()
            try:
                stale = repo.get_stale_sent(hours=24)
            finally:
                repo.close()
            assert str(pair[1]) not in {str(r.id) for r in stale}
        finally:
            _delete_rows(pair)

    def test_stamped_sweep_still_reaps_delivered(self):
        """A delivered card nobody acts on must age out via the existing reap
        — delivered is NOT a new stranding class."""
        pair = _create_queue_row(
            status="delivered", stamped=True, scheduled_age_hours=48
        )
        try:
            repo = QueueRepository()
            try:
                stale = repo.get_stale_sent(hours=24)
            finally:
                repo.close()
            assert str(pair[1]) in {str(r.id) for r in stale}
        finally:
            _delete_rows(pair)

    def test_unsent_sweep_still_covers_stale_unstamped_processing(self):
        """The coverage that makes deleting discard_abandoned_processing safe:
        an unstamped processing row past reap age is returned by the hourly
        sweep feed, whose consumer deletes through record_expiry_and_delete
        (history-first, #687) — strictly safer than discard's raw delete."""
        pair = _create_queue_row(
            status="processing", stamped=False, scheduled_age_hours=48
        )
        try:
            repo = QueueRepository()
            try:
                stale = repo.get_stale_unsent(hours=24)
            finally:
                repo.close()
            assert str(pair[1]) in {str(r.id) for r in stale}
        finally:
            _delete_rows(pair)
