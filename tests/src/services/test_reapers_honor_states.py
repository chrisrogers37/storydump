"""Integration tests: the scheduler's inline reap honors the delivery-state
machine (PR5).

A button-bearing card that ages out is ``delivered`` (not ``processing``)
under the delivery-state machine, and ``sent_unconfirmed`` belongs to the aged
reconcile, never the scheduler's generic reap. These drive the real
``_scheduler_tick`` over seeded rows and assert the reap is history-first
(#687). Repos are routed at the current-schema ``.env.test`` database (see
test_queue_sweep_status for the routing rationale); every row is deleted in
``finally``.
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
from src.repositories.tenant_scope import SYSTEM_SCOPE


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
            chat_settings_id=SYSTEM_SCOPE,
        )
        media_id = media.id
    finally:
        media_repo.close()

    queue_repo = QueueRepository()
    try:
        item = queue_repo.create(
            media_item_id=media_id,
            scheduled_for=datetime.utcnow() - timedelta(hours=scheduled_age_hours),
            chat_settings_id=SYSTEM_SCOPE,
        )
        queue_id = item.id
        item.status = status
        if stamped:
            item.telegram_message_id = 424242
            item.telegram_chat_id = 111111
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


def _delete_rows(pair) -> None:
    media_id, queue_id = pair
    queue_repo = QueueRepository()
    try:
        queue_repo.delete(str(queue_id))
    finally:
        queue_repo.close()
    media_repo = MediaRepository()
    try:
        media_repo.delete(str(media_id), chat_settings_id=SYSTEM_SCOPE)
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


async def _run_scheduler_reap(queue_repo) -> None:
    """Drive one scheduler tick's reap over the real DB: history writes go
    through a real HistoryRepository while the Telegram bot is a no-op (the
    reap edits the card, then writes history + deletes via the real repos).
    get_all_active_chats returns [] so the tick no-ops after the reap block."""
    # Close the reap's HistoryRepository deterministically (context manager)
    # rather than leaving it to __del__/GC: a session generator finalized at GC
    # time can segfault in the psycopg2/greenlet C layer under coverage tracing
    # (the fragile base_repository __del__ finalizer).
    with HistoryRepository() as history_repo:
        scheduler_service = Mock()
        scheduler_service.telegram_service.application.bot = AsyncMock()
        scheduler_service.history_repo = history_repo
        settings_service = Mock()
        settings_service.get_all_active_chats.return_value = []
        await _scheduler_tick(
            scheduler_service,
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
            await _run_scheduler_reap(queue_repo)

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
            status="sent_unconfirmed", stamped=False, scheduled_age_hours=48
        )
        _, queue_id = pair
        queue_repo = QueueRepository()
        try:
            await _run_scheduler_reap(queue_repo)

            row = _get_row(queue_id)
            assert row is not None, "sent_unconfirmed must be left for reconcile"
            assert row.status == "sent_unconfirmed"
            assert _history_for(queue_id) is None, "no expiry history should be written"
        finally:
            queue_repo.close()
            _delete_history_for(queue_id)
            _delete_rows(pair)
