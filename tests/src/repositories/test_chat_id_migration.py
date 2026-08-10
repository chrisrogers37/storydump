"""Chat-id migration: a group→supergroup migration must not strand its tenant (#743).

Telegram changes a chat's id when a group migrates to a supergroup. Every one of
these tests runs against a REAL database session, not a mock, because the defect
lives in the interaction between ``get_or_create`` and the UNIQUE constraint on
``chat_settings.telegram_chat_id`` — a mocked session cannot enforce that
constraint and would pass whatever the implementation did.

Models are imported at module scope on purpose: ``Base.metadata.create_all`` in
``tests/conftest.py`` only creates tables for models imported before it runs, so
a model imported inside a test function yields "relation does not exist" rather
than a skip.
"""

import uuid
from datetime import datetime

import pytest

from src.models.chat_settings import ChatSettings
from src.models.media_item import MediaItem
from src.models.posting_queue import PostingQueue
from src.models.user_interaction import UserInteraction
from src.repositories.chat_settings_repository import (
    ChatIdMigrationConflict,
    ChatSettingsRepository,
)

# A plain group id and the supergroup id Telegram would migrate it to. The
# supergroup form is the -100-prefixed one; the values matter only in that they
# differ and both fit BigInteger.
OLD_ID = -4_811_223_344
NEW_ID = -1_002_811_223_344


@pytest.fixture
def repo(test_db) -> ChatSettingsRepository:
    """ONE repository bound to the test session, held for the whole test.

    Deliberately a fixture rather than a per-call helper. BaseRepository closes
    its session in ``__del__``, so building a throwaway repo per call lets the
    garbage collector tear down the session the test is still using — which
    surfaced as DetachedInstanceError and as rows vanishing mid-test. One
    instance also matches how the app holds repositories (process-lifetime
    singletons).
    """
    instance = ChatSettingsRepository.__new__(ChatSettingsRepository)
    instance._db = test_db
    yield instance


def _tenant(session, chat_id: int, **overrides) -> ChatSettings:
    """A tenant with a NON-default marker, so 'the real one' is identifiable.

    posts_per_day is the marker throughout: a freshly minted blank carries the
    configured default, so a test that only checked "a row exists at the new id"
    would pass on exactly the bug being fixed.
    """
    fields = dict(
        telegram_chat_id=chat_id,
        dry_run_mode=False,
        enable_instagram_api=False,
        is_paused=False,
        posts_per_day=7,
        posting_hours_start=9,
        posting_hours_end=17,
        repost_ttl_days=30,
    )
    fields.update(overrides)
    row = ChatSettings(**fields)
    session.add(row)
    session.flush()
    return row


def _tenant_count(session) -> int:
    """Rows for THIS module's two ids only — the table is shared with other tests."""
    return (
        session.query(ChatSettings)
        .filter(ChatSettings.telegram_chat_id.in_([OLD_ID, NEW_ID]))
        .count()
    )


@pytest.fixture(autouse=True)
def _isolate(test_db):
    """Delete this module's rows before AND after each test.

    The ``test_db`` fixture isolates by ``begin_nested()`` + rollback, which is
    not sufficient here: every repository read ends with ``end_read_transaction``
    → ``commit()``, and committing releases the savepoint, so the rollback no
    longer undoes anything. Without this, rows leak between tests and the suite
    becomes order-dependent — one of these tests initially failed on a count of
    1 for a tenant a *previous* test had created.
    """

    def _wipe():
        test_db.query(PostingQueue).filter(
            PostingQueue.telegram_chat_id.in_([OLD_ID, NEW_ID])
        ).delete(synchronize_session=False)
        test_db.query(UserInteraction).filter(
            UserInteraction.telegram_chat_id.in_([OLD_ID, NEW_ID])
        ).delete(synchronize_session=False)
        test_db.query(ChatSettings).filter(
            ChatSettings.telegram_chat_id.in_([OLD_ID, NEW_ID])
        ).delete(synchronize_session=False)
        test_db.commit()

    _wipe()
    yield
    _wipe()


@pytest.mark.integration
class TestChatIdMigration:
    def test_tenant_follows_the_id_and_no_blank_is_minted(self, test_db, repo):
        """THE BUG. Without a handler, the first update from the new id mints a
        fresh default tenant and the real one strands on the dead id."""
        original = _tenant(test_db, OLD_ID)
        original_pk = original.id

        repo.migrate_chat_id(OLD_ID, NEW_ID)

        moved = repo.get_by_chat_id(NEW_ID)
        assert moved is not None, "tenant did not follow the migration"
        # Same row, not a look-alike: the surrogate key is what every other
        # table's FK points at, so identity here is the whole property.
        assert moved.id == original_pk
        assert moved.posts_per_day == 7, "settings did not survive the migration"
        # And nothing is left behind to be found by the old id.
        assert repo.get_by_chat_id(OLD_ID) is None

    def test_get_or_create_on_the_new_id_returns_the_migrated_tenant(
        self, test_db, repo
    ):
        """The access path the app actually uses. After migration, get_or_create
        must resolve the SAME tenant rather than bootstrap a blank."""
        original_pk = _tenant(test_db, OLD_ID).id
        repo.migrate_chat_id(OLD_ID, NEW_ID)

        resolved = repo.get_or_create(NEW_ID)

        assert resolved.id == original_pk
        assert resolved.posts_per_day == 7
        assert _tenant_count(test_db) == 1, "a second tenant was minted"

    def test_is_idempotent_because_telegram_sends_the_update_twice(self, test_db, repo):
        """PTB's wiki warns the migration update is delivered twice. A second
        application must be a no-op, not an error and not a duplicate row."""
        original_pk = _tenant(test_db, OLD_ID).id

        repo.migrate_chat_id(OLD_ID, NEW_ID)
        repo.migrate_chat_id(OLD_ID, NEW_ID)  # the duplicate

        assert repo.get_by_chat_id(NEW_ID).id == original_pk
        assert _tenant_count(test_db) == 1

    def test_unknown_old_id_is_a_no_op_not_a_mint(self, test_db, repo):
        """A migration signal for a chat this bot never had settings for must
        not conjure a tenant — get_or_create is the only thing allowed to."""
        repo.migrate_chat_id(OLD_ID, NEW_ID)
        assert _tenant_count(test_db) == 0

    def test_queued_work_follows_the_migration(self, test_db, repo):
        """posting_queue carries a DENORMALIZED raw chat id and filters on it
        (queue_repository.py:312,544), so queued posts strand invisibly unless
        the migration sweeps them too."""
        tenant = _tenant(test_db, OLD_ID)
        media = MediaItem(
            file_path=f"/tmp/{uuid.uuid4()}.jpg",
            file_name="x.jpg",
            file_size=1,
            file_hash=uuid.uuid4().hex,
        )
        test_db.add(media)
        test_db.flush()
        test_db.add(
            PostingQueue(
                media_item_id=media.id,
                chat_settings_id=tenant.id,
                telegram_chat_id=OLD_ID,
                scheduled_for=datetime.utcnow(),
                status="pending",
            )
        )
        test_db.flush()

        repo.migrate_chat_id(OLD_ID, NEW_ID)

        rows = test_db.query(PostingQueue).all()
        assert len(rows) == 1
        assert rows[0].telegram_chat_id == NEW_ID, "queued work stranded on the dead id"

    def test_interaction_history_follows_the_migration(self, test_db, repo):
        """user_interactions carries the raw id and NOTHING else tenant-shaped
        (user_interaction.py:48), so analytics fork across the id boundary."""
        _tenant(test_db, OLD_ID)
        test_db.add(
            UserInteraction(
                telegram_chat_id=OLD_ID,
                interaction_type="command",
                interaction_name="/status",
            )
        )
        test_db.flush()

        repo.migrate_chat_id(OLD_ID, NEW_ID)

        rows = test_db.query(UserInteraction).all()
        assert len(rows) == 1
        assert rows[0].telegram_chat_id == NEW_ID, "interaction history forked"

    def test_refuses_when_a_tenant_already_exists_at_the_new_id(self, test_db, repo):
        """THE RECOVERY CASE, and the one that decides the design.

        If the migration already happened, get_or_create has already minted a
        blank tenant at the new id — so the backstop fires against a UNIQUE
        column that is now occupied. Re-pointing blindly violates the
        constraint; picking a winner silently destroys one of two real tenants,
        which is the same data loss this fix exists to stop.

        So it must refuse, loudly and without writing. Merging is a judgement
        call about which settings survive, and that is not a handler's to make.
        """
        old_pk = _tenant(test_db, OLD_ID).id
        new_pk = _tenant(test_db, NEW_ID, posts_per_day=3).id

        with pytest.raises(ChatIdMigrationConflict):
            repo.migrate_chat_id(OLD_ID, NEW_ID)

        # Neither row was touched.
        assert repo.get_by_chat_id(OLD_ID).id == old_pk
        assert repo.get_by_chat_id(NEW_ID).id == new_pk
        assert repo.get_by_chat_id(NEW_ID).posts_per_day == 3


@pytest.mark.unit
class TestMigrationHandlerWiring:
    """The handler is what makes the repository fix fire in production.

    Unit-level with a mocked service: this asserts the SIGNAL is read correctly
    (which id is old, which is new) and that a conflict is not allowed to
    escape into PTB. The data movement itself is covered against a real
    database above.
    """

    def _handler(self, service):
        from src.services.core.telegram_membership import TelegramMembershipHandler

        return TelegramMembershipHandler(service)

    @pytest.mark.asyncio
    async def test_reads_old_and_new_ids_the_right_way_round(self):
        from unittest.mock import MagicMock

        service = MagicMock()
        update = MagicMock()
        # Telegram puts the service message on the NEW chat, carrying the OLD id.
        update.effective_message.migrate_from_chat_id = OLD_ID
        update.effective_message.chat.id = NEW_ID

        await self._handler(service).handle_chat_migration(update, None)

        service.settings_service.migrate_chat_id.assert_called_once_with(OLD_ID, NEW_ID)

    @pytest.mark.asyncio
    async def test_ignores_a_message_that_is_not_a_migration(self):
        from unittest.mock import MagicMock

        service = MagicMock()
        update = MagicMock()
        update.effective_message.migrate_from_chat_id = None

        await self._handler(service).handle_chat_migration(update, None)

        service.settings_service.migrate_chat_id.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_conflict_is_logged_not_raised_into_ptb(self):
        """An exception escaping here reaches PTB's error handler and the
        migration is simply lost; the operator needs a log line naming both
        ids so the two tenants can be reconciled by hand."""
        from unittest.mock import MagicMock

        service = MagicMock()
        service.settings_service.migrate_chat_id.side_effect = ChatIdMigrationConflict(
            "occupied"
        )
        update = MagicMock()
        update.effective_message.migrate_from_chat_id = OLD_ID
        update.effective_message.chat.id = NEW_ID

        await self._handler(service).handle_chat_migration(
            update, None
        )  # must not raise
