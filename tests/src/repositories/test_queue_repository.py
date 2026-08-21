"""Tests for QueueRepository."""

import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy.orm import Session

from src.repositories.queue_repository import QueueRepository
from src.models.posting_queue import PostingQueue
from src.repositories.tenant_scope import SYSTEM_SCOPE


@pytest.fixture
def mock_db():
    """Create a mock database session with chainable query."""
    session = MagicMock(spec=Session)
    mock_query = MagicMock()
    session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.order_by.return_value = mock_query
    mock_query.limit.return_value = mock_query
    return session


@pytest.fixture
def queue_repo(mock_db):
    """Create QueueRepository with mocked database session."""
    with patch.object(QueueRepository, "__init__", lambda self: None):
        repo = QueueRepository()
        repo._db = mock_db
        return repo


@pytest.mark.unit
class TestQueueRepository:
    """Test suite for QueueRepository."""

    def test_create_queue_item(self, queue_repo, mock_db):
        """Test creating a new queue item."""
        scheduled_time = datetime.utcnow() + timedelta(hours=1)
        media_item_id = str(uuid4())

        queue_repo.create(
            media_item_id=media_item_id,
            scheduled_for=scheduled_time,
            chat_settings_id=SYSTEM_SCOPE,
        )

        mock_db.add.assert_called_once()
        mock_db.commit.assert_called()
        mock_db.refresh.assert_called_once()

        added_item = mock_db.add.call_args[0][0]
        assert isinstance(added_item, PostingQueue)
        assert added_item.media_item_id == media_item_id
        assert added_item.scheduled_for == scheduled_time

    def test_get_pending_items(self, queue_repo, mock_db):
        """Test retrieving pending queue items."""
        mock_items = [MagicMock(status="pending"), MagicMock(status="pending")]
        # Build a self-referential mock chain so filter/order_by/with_for_update
        # all return the same mock, and .all() returns mock_items
        mock_query = mock_db.query.return_value
        mock_query.filter.return_value = mock_query
        mock_query.order_by.return_value = mock_query
        mock_query.limit.return_value = mock_query
        mock_query.with_for_update.return_value = mock_query
        mock_query.all.return_value = mock_items

        result = queue_repo.get_pending(chat_settings_id=SYSTEM_SCOPE)

        assert len(result) == 2
        mock_db.query.assert_called_with(PostingQueue)

    def test_get_stale_unsent_only_targets_unstamped_rows(self, queue_repo, mock_db):
        """get_stale_unsent(hours=24) is a read — it returns the unstamped
        (telegram_message_id IS NULL) accumulation and deletes NOTHING.

        Deletion moved to the service layer (#687): each returned row gets a
        terminal 'expired' history row via record_expiry_and_delete before it
        is deleted, so a delivered-but-unstamped card (#679/#680) degrades to
        "Expired" on tap instead of the raw "Queue item not found".
        """
        stale_item_a = MagicMock()
        stale_item_a.scheduled_for = datetime.utcnow() - timedelta(days=10)
        stale_item_b = MagicMock()
        stale_item_b.scheduled_for = datetime.utcnow() - timedelta(days=2)
        mock_query = mock_db.query.return_value
        mock_query.filter.return_value = mock_query
        mock_query.order_by.return_value = mock_query
        mock_query.all.return_value = [stale_item_a, stale_item_b]

        result = queue_repo.get_stale_unsent(hours=24)

        assert result == [stale_item_a, stale_item_b]
        mock_db.delete.assert_not_called()
        # Scoped to unstamped rows; stamped cards go through expire_sent_row.
        filter_args = mock_query.filter.call_args[0]
        assert any("telegram_message_id IS NULL" in str(a) for a in filter_args)

    def test_get_stale_unsent_empty_when_no_stale_items(self, queue_repo, mock_db):
        """Nothing past the cutoff → empty list, nothing deleted."""
        mock_query = mock_db.query.return_value
        mock_query.filter.return_value = mock_query
        mock_query.order_by.return_value = mock_query
        mock_query.all.return_value = []

        result = queue_repo.get_stale_unsent(hours=24)

        assert result == []
        mock_db.delete.assert_not_called()

    def test_delete_queue_item(self, queue_repo, mock_db):
        """Test deleting a queue item."""
        mock_item = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = mock_item

        result = queue_repo.delete("some-id", SYSTEM_SCOPE)

        assert result is True
        mock_db.delete.assert_called_once_with(mock_item)
        # commit called twice: once by get_by_id's end_read_transaction, once by the write
        assert mock_db.commit.call_count == 2

    def test_delete_queue_item_not_found(self, queue_repo, mock_db):
        """Test deleting a non-existent queue item."""
        mock_db.query.return_value.filter.return_value.first.return_value = None

        result = queue_repo.delete("nonexistent-id", SYSTEM_SCOPE)

        assert result is False
        mock_db.delete.assert_not_called()

    def test_get_all_queue_items(self, queue_repo, mock_db):
        """Test listing all queue items."""
        mock_items = [MagicMock(), MagicMock()]
        mock_query = mock_db.query.return_value
        mock_query.all.return_value = mock_items

        result = queue_repo.get_all(chat_settings_id=SYSTEM_SCOPE)

        assert len(result) == 2
        mock_db.query.assert_called_with(PostingQueue)

    def test_get_by_media_id(self, queue_repo, mock_db):
        """Test retrieving queue item by media ID."""
        media_id = str(uuid4())
        mock_item = MagicMock(media_item_id=media_id)
        mock_db.query.return_value.filter.return_value.first.return_value = mock_item

        result = queue_repo.get_by_media_id(media_id, chat_settings_id=SYSTEM_SCOPE)

        assert result is mock_item
        assert result.media_item_id == media_id

    def test_count_pending(self, queue_repo, mock_db):
        """Test counting pending items."""
        mock_db.query.return_value.filter.return_value.count.return_value = 5

        result = queue_repo.count_pending(chat_settings_id=SYSTEM_SCOPE)

        assert result == 5


@pytest.mark.unit
class TestQueueRepositoryTenantFiltering:
    """Tests for optional chat_settings_id tenant filtering on QueueRepository."""

    TENANT_ID = "tenant-uuid-1"

    def test_get_by_id_with_tenant(self, queue_repo, mock_db):
        """get_by_id passes chat_settings_id through tenant filter."""
        with patch.object(
            queue_repo, "_apply_tenant_filter", wraps=queue_repo._apply_tenant_filter
        ) as mock_filter:
            queue_repo.get_by_id("some-id", chat_settings_id=self.TENANT_ID)
            mock_filter.assert_called_once()
            assert mock_filter.call_args[0][2] == self.TENANT_ID

    def test_get_by_id_with_system_scope(self, queue_repo, mock_db):
        """Deliberate cross-tenant read: explicit SYSTEM_SCOPE, never omission (F.1/#841)."""
        with patch.object(
            queue_repo, "_apply_tenant_filter", wraps=queue_repo._apply_tenant_filter
        ) as mock_filter:
            queue_repo.get_by_id("some-id", chat_settings_id=SYSTEM_SCOPE)
            mock_filter.assert_called_once()
            assert mock_filter.call_args[0][2] is SYSTEM_SCOPE

    def test_get_by_id_prefix_with_tenant(self, queue_repo, mock_db):
        """get_by_id_prefix passes chat_settings_id through."""
        with patch.object(
            queue_repo, "_apply_tenant_filter", wraps=queue_repo._apply_tenant_filter
        ) as mock_filter:
            queue_repo.get_by_id_prefix("abcd1234", chat_settings_id=self.TENANT_ID)
            mock_filter.assert_called_once()
            assert mock_filter.call_args[0][2] == self.TENANT_ID

    def test_get_by_media_id_with_tenant(self, queue_repo, mock_db):
        """get_by_media_id passes chat_settings_id through."""
        with patch.object(
            queue_repo, "_apply_tenant_filter", wraps=queue_repo._apply_tenant_filter
        ) as mock_filter:
            queue_repo.get_by_media_id("media-123", chat_settings_id=self.TENANT_ID)
            mock_filter.assert_called_once()
            assert mock_filter.call_args[0][2] == self.TENANT_ID

    def test_get_pending_with_tenant(self, queue_repo, mock_db):
        """get_pending passes chat_settings_id through."""
        mock_db.query.return_value.all.return_value = []
        with patch.object(
            queue_repo, "_apply_tenant_filter", wraps=queue_repo._apply_tenant_filter
        ) as mock_filter:
            queue_repo.get_pending(chat_settings_id=self.TENANT_ID)
            mock_filter.assert_called_once()
            assert mock_filter.call_args[0][2] == self.TENANT_ID

    def test_get_all_with_tenant(self, queue_repo, mock_db):
        """get_all passes chat_settings_id through."""
        mock_db.query.return_value.all.return_value = []
        with patch.object(
            queue_repo, "_apply_tenant_filter", wraps=queue_repo._apply_tenant_filter
        ) as mock_filter:
            queue_repo.get_all(chat_settings_id=self.TENANT_ID)
            mock_filter.assert_called_once()
            assert mock_filter.call_args[0][2] == self.TENANT_ID

    def test_count_pending_with_tenant(self, queue_repo, mock_db):
        """count_pending passes chat_settings_id through."""
        with patch.object(
            queue_repo, "_apply_tenant_filter", wraps=queue_repo._apply_tenant_filter
        ) as mock_filter:
            queue_repo.count_pending(chat_settings_id=self.TENANT_ID)
            mock_filter.assert_called_once()
            assert mock_filter.call_args[0][2] == self.TENANT_ID

    def test_get_oldest_pending_with_tenant(self, queue_repo, mock_db):
        """get_oldest_pending passes chat_settings_id through."""
        with patch.object(
            queue_repo, "_apply_tenant_filter", wraps=queue_repo._apply_tenant_filter
        ) as mock_filter:
            queue_repo.get_oldest_pending(chat_settings_id=self.TENANT_ID)
            mock_filter.assert_called_once()
            assert mock_filter.call_args[0][2] == self.TENANT_ID

    def test_create_with_tenant(self, queue_repo, mock_db):
        """create sets chat_settings_id on the new PostingQueue item."""
        scheduled_time = datetime.utcnow() + timedelta(hours=1)
        queue_repo.create(
            media_item_id="media-123",
            scheduled_for=scheduled_time,
            chat_settings_id=self.TENANT_ID,
        )

        added_item = mock_db.add.call_args[0][0]
        assert added_item.chat_settings_id == self.TENANT_ID

    def test_create_without_tenant(self, queue_repo, mock_db):
        """create without chat_settings_id sets None (backward compat)."""
        scheduled_time = datetime.utcnow() + timedelta(hours=1)
        queue_repo.create(
            media_item_id="media-123",
            scheduled_for=scheduled_time,
            chat_settings_id=SYSTEM_SCOPE,
        )

        added_item = mock_db.add.call_args[0][0]
        assert added_item.chat_settings_id is None

    def test_delete_all_pending_with_tenant(self, queue_repo, mock_db):
        """delete_all_pending passes chat_settings_id through."""
        with patch.object(
            queue_repo, "_apply_tenant_filter", wraps=queue_repo._apply_tenant_filter
        ) as mock_filter:
            queue_repo.delete_all_pending(chat_settings_id=self.TENANT_ID)
            mock_filter.assert_called_once()
            assert mock_filter.call_args[0][2] == self.TENANT_ID


@pytest.mark.unit
class TestGetAllWithMedia:
    """Tests for get_all_with_media JOIN method."""

    def test_returns_tuples_with_media_info(self, queue_repo, mock_db):
        """get_all_with_media returns (PostingQueue, file_name, category) tuples."""
        mock_item = MagicMock(spec=PostingQueue)
        mock_query = mock_db.query.return_value
        mock_query.outerjoin.return_value = mock_query
        mock_query.add_columns.return_value = mock_query
        mock_query.filter.return_value = mock_query
        mock_query.order_by.return_value = mock_query
        mock_query.all.return_value = [(mock_item, "story.jpg", "memes")]

        result = queue_repo.get_all_with_media(
            status="pending", chat_settings_id=SYSTEM_SCOPE
        )

        assert len(result) == 1
        item, file_name, category = result[0]
        assert item is mock_item
        assert file_name == "story.jpg"
        assert category == "memes"

    def test_filters_by_status(self, queue_repo, mock_db):
        """get_all_with_media applies status filter when provided."""
        mock_query = mock_db.query.return_value
        mock_query.outerjoin.return_value = mock_query
        mock_query.add_columns.return_value = mock_query
        mock_query.filter.return_value = mock_query
        mock_query.order_by.return_value = mock_query
        mock_query.all.return_value = []

        queue_repo.get_all_with_media(
            status="processing", chat_settings_id=SYSTEM_SCOPE
        )

        mock_query.filter.assert_called()

    def test_no_status_filter(self, queue_repo, mock_db):
        """get_all_with_media works without status filter."""
        mock_query = mock_db.query.return_value
        mock_query.outerjoin.return_value = mock_query
        mock_query.add_columns.return_value = mock_query
        mock_query.order_by.return_value = mock_query
        mock_query.all.return_value = []

        queue_repo.get_all_with_media(chat_settings_id=SYSTEM_SCOPE)

        # Should still call order_by and all
        mock_query.order_by.assert_called()
        mock_query.all.assert_called_once()

    def test_calls_end_read_transaction(self, queue_repo, mock_db):
        """get_all_with_media calls end_read_transaction after fetching."""
        mock_query = mock_db.query.return_value
        mock_query.outerjoin.return_value = mock_query
        mock_query.add_columns.return_value = mock_query
        mock_query.order_by.return_value = mock_query
        mock_query.all.return_value = []

        with patch.object(queue_repo, "end_read_transaction") as mock_end:
            queue_repo.get_all_with_media(chat_settings_id=SYSTEM_SCOPE)
            mock_end.assert_called_once()

    def test_passes_tenant_filter(self, queue_repo, mock_db):
        """get_all_with_media passes chat_settings_id through tenant filter."""
        mock_query = mock_db.query.return_value
        mock_query.outerjoin.return_value = mock_query
        mock_query.add_columns.return_value = mock_query
        mock_query.order_by.return_value = mock_query
        mock_query.all.return_value = []

        with patch.object(
            queue_repo, "_apply_tenant_filter", wraps=queue_repo._apply_tenant_filter
        ) as mock_filter:
            queue_repo.get_all_with_media(chat_settings_id="tenant-uuid-1")
            mock_filter.assert_called_once()
            assert mock_filter.call_args[0][2] == "tenant-uuid-1"


@pytest.mark.unit
class TestClaimForProcessing:
    """Tests for atomic claim_for_processing method."""

    def test_claim_for_processing_returns_item(self, queue_repo, mock_db):
        """claim_for_processing returns the item and sets status to processing."""
        mock_item = MagicMock()
        mock_item.status = "pending"
        mock_query = mock_db.query.return_value
        mock_query.filter.return_value = mock_query
        mock_query.with_for_update.return_value = mock_query
        mock_query.first.return_value = mock_item

        result = queue_repo.claim_for_processing("some-id")

        assert result is mock_item
        assert mock_item.status == "processing"
        mock_db.commit.assert_called()

    def test_claim_for_processing_returns_none_when_missing(self, queue_repo, mock_db):
        """claim_for_processing returns None when item doesn't exist."""
        mock_query = mock_db.query.return_value
        mock_query.filter.return_value = mock_query
        mock_query.with_for_update.return_value = mock_query
        mock_query.first.return_value = None

        result = queue_repo.claim_for_processing("nonexistent")

        assert result is None
        mock_db.commit.assert_not_called()


@pytest.mark.unit
class TestGetStaleSent:
    """Tests for get_stale_sent (button-bearing rows past reap age, #560)."""

    def test_returns_rows_with_status_filter(self, queue_repo, mock_db):
        """With a status filter, applies base + status filter and returns rows."""
        rows = [MagicMock(), MagicMock()]
        mock_query = mock_db.query.return_value
        mock_query.filter.return_value = mock_query
        mock_query.order_by.return_value = mock_query
        mock_query.all.return_value = rows

        result = queue_repo.get_stale_sent(hours=24, status="processing")

        assert result == rows
        mock_db.query.assert_called_with(PostingQueue)
        # base filter (msg_id NOT NULL + age) then the optional status filter
        assert mock_query.filter.call_count == 2
        mock_query.order_by.assert_called_once()

    def test_scopes_to_sent_rows_without_status(self, queue_repo, mock_db):
        """Without a status filter, a single filter scopes to sent rows only."""
        mock_query = mock_db.query.return_value
        mock_query.filter.return_value = mock_query
        mock_query.order_by.return_value = mock_query
        mock_query.all.return_value = []

        queue_repo.get_stale_sent(hours=12)

        assert mock_query.filter.call_count == 1
        filter_args = mock_query.filter.call_args[0]
        assert any("telegram_message_id IS NOT NULL" in str(a) for a in filter_args)

    def test_calls_end_read_transaction(self, queue_repo, mock_db):
        """get_stale_sent ends the read transaction after fetching."""
        mock_query = mock_db.query.return_value
        mock_query.filter.return_value = mock_query
        mock_query.order_by.return_value = mock_query
        mock_query.all.return_value = []

        with patch.object(queue_repo, "end_read_transaction") as mock_end:
            queue_repo.get_stale_sent()
            mock_end.assert_called_once()


def _literal(expr) -> str:
    """Render a SQLAlchemy filter expression with its bound values inlined."""
    return str(expr.compile(compile_kwargs={"literal_binds": True}))


@pytest.mark.unit
class TestMarkPublishing:
    """QueueRepository.mark_publishing — the claim-before-publish signal (#549)."""

    def test_sets_status_and_container_id(self, queue_repo, mock_db):
        """mark_publishing flips status to 'publishing' and persists the container id."""
        mock_item = MagicMock()
        mock_item.status = "pending"
        mock_item.instagram_container_id = None
        mock_db.query.return_value.filter.return_value.first.return_value = mock_item

        result = queue_repo.mark_publishing("q-1", "container-abc", SYSTEM_SCOPE)

        assert result is mock_item
        assert mock_item.status == "publishing"
        assert mock_item.instagram_container_id == "container-abc"
        mock_db.refresh.assert_called_once_with(mock_item)

    def test_no_op_when_missing(self, queue_repo, mock_db):
        """mark_publishing returns None when the row is gone."""
        mock_db.query.return_value.filter.return_value.first.return_value = None
        assert queue_repo.mark_publishing("nope", "c", SYSTEM_SCOPE) is None


@pytest.mark.unit
class TestSweepsExcludePublishing:
    """Every stale-sweep/reaper must leave a 'publishing' row intact so a
    claimed-but-unconfirmed publish is never reaped and re-served (#549)."""

    def test_get_stale_unsent_excludes_publishing(self, queue_repo, mock_db):
        """get_stale_unsent (targets msg_id IS NULL regardless of status after
        #561) must additionally exclude status='publishing'."""
        mock_query = mock_db.query.return_value
        mock_query.filter.return_value = mock_query
        mock_query.order_by.return_value = mock_query
        mock_query.all.return_value = []

        queue_repo.get_stale_unsent(hours=24)

        filter_args = mock_query.filter.call_args[0]
        rendered = " ".join(_literal(a) for a in filter_args)
        assert "status != 'publishing'" in rendered
        assert "telegram_message_id IS NULL" in rendered

    def test_get_stale_sent_excludes_publishing(self, queue_repo, mock_db):
        """get_stale_sent feeds the button-bearing reaper (expire_sent_row);
        a stuck 'publishing' autopost card (msg_id NOT NULL) must be excluded."""
        mock_query = mock_db.query.return_value
        mock_query.filter.return_value = mock_query
        mock_query.order_by.return_value = mock_query
        mock_query.all.return_value = []

        queue_repo.get_stale_sent(hours=24)

        filter_args = mock_query.filter.call_args[0]
        rendered = " ".join(_literal(a) for a in filter_args)
        assert "status != 'publishing'" in rendered
        assert "telegram_message_id IS NOT NULL" in rendered

    def test_get_stale_unsent_pending_only_targets_pending(self, queue_repo, mock_db):
        """get_stale_unsent_pending is status-scoped to unstamped 'pending'
        rows — a 'publishing' row can never match, so it needs no extra
        guard. A read: deletion (with its history write) is the caller's."""
        mock_query = mock_db.query.return_value
        mock_query.filter.return_value = mock_query
        mock_query.order_by.return_value = mock_query
        mock_query.all.return_value = []

        queue_repo.get_stale_unsent_pending(max_age_minutes=10)

        mock_db.delete.assert_not_called()
        filter_args = mock_query.filter.call_args[0]
        rendered = " ".join(_literal(a) for a in filter_args)
        assert "status = 'pending'" in rendered
        assert "telegram_message_id IS NULL" in rendered
        assert "publishing" not in rendered

    def test_processing_sweep_only_targets_processing(self, queue_repo, mock_db):
        """resolve_stale_processing selects on status + age ONLY (INV-2):
        'publishing' rows are excluded by construction, and the stamp carries
        no selection meaning — disposition reads it, the WHERE never does."""
        mock_db.query.return_value.filter.return_value.all.return_value = []
        queue_repo.resolve_stale_processing(max_age_minutes=10)
        rendered = " ".join(
            _literal(a) for a in mock_db.query.return_value.filter.call_args[0]
        )
        assert "status = 'processing'" in rendered
        assert "publishing" not in rendered
        assert "telegram_message_id" not in rendered
