"""Integration tests: the DB-level uniqueness backstop on posting_history.

Real DB, deliberately. The thing under test is a partial unique index, and the
only way to observe it is to make Postgres reject a write. A test asserting the
``Index`` object exists in ``__table_args__`` would pass against a declaration
that never reaches a database — the exact defect this constraint exists to close
was an application-level guard that looked correct and had no DB behind it.

The test DB is built by ``Base.metadata.create_all``, so these tests fail
without the model declaration and pass with it: the index they observe is the
one the model produces.
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from src.models.media_item import MediaItem
from src.models.posting_history import PostingHistory


def _media_item(session) -> str:
    """A real media_items row — media_item_id is a live FK."""
    item = MediaItem(
        id=uuid.uuid4(),
        file_path=f"/test/media/{uuid.uuid4()}.jpg",
        file_name="image.jpg",
        file_size=1024,
        file_hash=uuid.uuid4().hex,
        source_type="local",
    )
    session.add(item)
    session.flush()
    return str(item.id)


def _history(media_id: str, queue_item_id) -> PostingHistory:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return PostingHistory(
        id=uuid.uuid4(),
        media_item_id=media_id,
        queue_item_id=queue_item_id,
        queue_created_at=now,
        queue_deleted_at=now,
        scheduled_for=now,
        posted_at=now,
        status="posted",
        success=True,
        posting_method="telegram_manual",
    )


@pytest.mark.integration
class TestUniqueQueueItemId:
    """One terminal history row per queue item, enforced by the database."""

    def test_two_rows_with_the_same_queue_item_id_are_rejected(self, test_db):
        """The replayed-finalize case: the second write must not land."""
        media_id = _media_item(test_db)
        queue_item_id = str(uuid.uuid4())

        test_db.add(_history(media_id, queue_item_id))
        test_db.flush()

        test_db.add(_history(media_id, queue_item_id))
        with pytest.raises(IntegrityError) as excinfo:
            test_db.flush()

        assert "uq_posting_history_queue_item_id" in str(excinfo.value)

    def test_many_rows_with_a_null_queue_item_id_are_allowed(self, test_db):
        """History outlives its queue rows; NULL links must stay unconstrained."""
        media_id = _media_item(test_db)

        for _ in range(3):
            test_db.add(_history(media_id, None))
        test_db.flush()

        count = (
            test_db.query(PostingHistory)
            .filter(
                PostingHistory.media_item_id == media_id,
                PostingHistory.queue_item_id.is_(None),
            )
            .count()
        )
        assert count == 3

    def test_distinct_queue_item_ids_are_unaffected(self, test_db):
        """A positive control: the constraint must not reject normal writes.

        Without this, a test suite that only ever observes rejection would pass
        against an index that rejects everything.
        """
        media_id = _media_item(test_db)

        for _ in range(3):
            test_db.add(_history(media_id, str(uuid.uuid4())))
        test_db.flush()

        count = (
            test_db.query(PostingHistory)
            .filter(
                PostingHistory.media_item_id == media_id,
                PostingHistory.queue_item_id.isnot(None),
            )
            .count()
        )
        assert count == 3
