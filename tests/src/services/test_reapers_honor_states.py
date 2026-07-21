"""Integration tests: the two 24h reapers honor the delivery-state machine.

PR5 of the data-model redesign. PR3/PR4 already made the hourly cleanup loop
(``cleanup_queue_loop``) honor ``delivered`` (stamped sweep), leave
``sent_unconfirmed`` to the aged reconcile, and write terminal ``expired``
history before every delete (#687). This file locks in that the scheduler's
inline reap (``_scheduler_tick``) honors the same states:

* An aged ``delivered`` card nobody acted on is expired history-first — under
  the delivery-state machine a button-bearing stuck row is ``delivered`` (the
  stamp promotes ``processing`` -> ``delivered``; ``resolve_stale_processing``
  empties ``processing`` at 10 min), so a ``status="processing"`` filter reaps
  nothing it exists to reap.
* A ``sent_unconfirmed`` row is left untouched — its lifecycle belongs to the
  aged reconcile (PR4), never the scheduler's generic reap.

Real DB: repos are routed at the current-schema ``.env.test`` database (same
pattern as test_queue_sweep_status); every row is deleted in ``finally``.
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy.orm import sessionmaker

from src.repositories.history_repository import HistoryRepository
from src.repositories.media_repository import MediaRepository
from src.repositories.queue_repository import QueueRepository
from src.services.core.loops.scheduler_loop import _scheduler_tick


@pytest.fixture(autouse=True)
def _route_repos_to_test_db(setup_test_database, monkeypatch):
    """Route the production repo session factory at the current-schema test DB
    (see test_queue_sweep_status for the full rationale)."""
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
    *, status: str, stamped: bool, scheduled_age_hours: float
) -> tuple:
    """Create a media_item + posting_queue row in the given lifecycle shape.

    Returns ``(media_id, queue_id)``; caller MUST clean up via ``_delete_rows``.
    ``scheduled_age_hours`` backdates ``scheduled_for`` (the reap predicate).
    """
    media_repo = MediaRepository()
    try:
        media = media_repo.create(
            file_path=f"/test/reapers-honor-states/{uuid4()}.jpg",
            file_name="reaper.jpg",
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
        queue_repo.db.commit()
    finally:
        queue_repo.close()

    return media_id, queue_id


def _delete_history_for(queue_id) -> None:
    repo = HistoryRepository()
    try:
        row = repo.get_by_queue_item_id(str(queue_id))
        if row:
            repo.db.delete(row)
            repo.db.commit()
    finally:
        repo.close()


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


def _get_row(queue_id):
    repo = QueueRepository()
    try:
        return repo.get_by_id(str(queue_id))
    finally:
        repo.close()


def _history_for(queue_id):
    repo = HistoryRepository()
    try:
        return repo.get_by_queue_item_id(str(queue_id))
    finally:
        repo.close()


def _scheduler_service_with_real_history():
    """A scheduler_service double whose reap dependencies are real (history_repo)
    but whose Telegram bot is a no-op — the reap edits a card then writes history
    + deletes through the real repos."""
    svc = Mock()
    svc.telegram_service.application.bot = AsyncMock()
    svc.history_repo = HistoryRepository()
    return svc


async def _run_reaper_a(queue_repo) -> None:
    """Drive one scheduler tick's reap over the real DB. get_all_active_chats
    returns [] so the tick no-ops after the reap block."""
    settings_service = Mock()
    settings_service.get_all_active_chats.return_value = []
    await _scheduler_tick(
        _scheduler_service_with_real_history(),
        Mock(),  # posting_service — unused once active_chats is empty
        settings_service,
        queue_repo,
        first_tick=False,
    )


@pytest.mark.integration
@pytest.mark.asyncio
class TestSchedulerReapHonorsStates:
    async def test_reaps_aged_delivered_row_history_first(self):
        """A delivered card nobody acted on ages out via the scheduler reap:
        terminal 'expired' history is written BEFORE the queue row is deleted
        (#687), so a late tap shows 'Expired', not 'Queue item not found'."""
        pair = _create_queue_row(
            status="delivered", stamped=True, scheduled_age_hours=48
        )
        _, queue_id = pair
        queue_repo = QueueRepository()
        try:
            await _run_reaper_a(queue_repo)

            assert _get_row(queue_id) is None, "delivered row should be reaped"
            hist = _history_for(queue_id)
            assert hist is not None, "history must be written before delete (#687)"
            assert hist.status == "expired"
            assert hist.success is False
            assert hist.posting_method == "system_expiry"
        finally:
            queue_repo.close()
            _delete_history_for(queue_id)
            _delete_rows(pair)

    async def test_leaves_sent_unconfirmed_for_reconcile(self):
        """sent_unconfirmed is the one delivery state the scheduler reap must
        NOT touch — its lifecycle belongs to the aged reconcile (PR4). Reaping
        it here would race that purpose-built path."""
        pair = _create_queue_row(
            status="sent_unconfirmed", stamped=True, scheduled_age_hours=48
        )
        _, queue_id = pair
        queue_repo = QueueRepository()
        try:
            await _run_reaper_a(queue_repo)

            row = _get_row(queue_id)
            assert row is not None, "sent_unconfirmed must be left for reconcile"
            assert row.status == "sent_unconfirmed"
            assert _history_for(queue_id) is None, "no expiry history should be written"
        finally:
            queue_repo.close()
            _delete_history_for(queue_id)
            _delete_rows(pair)
