"""The ChatMigrated send-path signal must reach a durable store (#743).

A group->supergroup migration changes the chat id. #744 handles the live
service message; this covers the OTHER channel Telegram signals on — the
permanent send-path error — which is the only one that can still reach a chat
stranded before #744 shipped.

Before this change the signal arrived on every scheduler tick and was thrown
away: ``send_notification`` caught it in a blanket ``except Exception``, logged
it as a generic string and returned False, so the scheduler recorded the
literal ``"send_notification returned False"``. The new chat id — the one fact
needed to recover the tenant — existed only inside a log line and aged out with
log retention.

These tests assert the pair survives into ``posting_history.error_message``,
the durable column that already receives that string. No schema change, and no
recovery policy presupposed: recording the pair is what every candidate
recovery option needs, so it is safe to land while C-vs-D is still open.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from telegram.error import ChatMigrated

from src.exceptions.telegram import ChatMigratedError
from src.models.posting_history import PostingHistory
from src.repositories.history_repository import HistoryCreateParams, HistoryRepository
from src.services.core.scheduler import SchedulerService
from src.services.core.telegram_notification import TelegramNotificationService
from tests.src.services.conftest import mock_track_execution

# A plain group id and the supergroup id Telegram migrates it to. Matches the
# constants in tests/src/repositories/test_chat_id_migration.py so the two
# halves of #743 are readable against each other.
OLD_ID = -4_811_223_344
NEW_ID = -1_002_811_223_344


# ------------------------------------------------------------------
# The durable format itself
# ------------------------------------------------------------------


@pytest.mark.unit
class TestDurableFormat:
    """Render and parse are inverses, and parse refuses to guess."""

    def test_round_trips_the_pair(self):
        """The pair written is the pair recovered.

        This is the property the whole change rests on. A durable message that
        cannot be parsed back is the same as no durable message: the recovery
        pass would still have nothing to act on.

        Both constants are negative, which is the realistic case rather than an
        edge one — groups are negative and supergroups -100-prefixed — so a
        pattern that dropped the sign fails here rather than needing its own
        test.
        """
        message = ChatMigratedError(OLD_ID, NEW_ID).durable_message()

        assert ChatMigratedError.parse_pair(message) == (OLD_ID, NEW_ID)
        assert OLD_ID < 0 and NEW_ID < 0

    @pytest.mark.parametrize(
        "other_message",
        [
            None,
            "",
            "send_notification returned False",
            "GoogleDriveAuthError: credentials invalid",
            "chat_migrated old_chat_id=-4811223344",  # half a pair
        ],
    )
    def test_returns_none_rather_than_guessing(self, other_message):
        """Any non-migration failure yields None, including a partial match.

        A sweep over posting_history sees every send failure, so parse must
        discriminate rather than assume. A half-built pair would be worse than
        none: re-pointing a live tenant at a wrongly-parsed id is the data loss
        this whole issue is about.
        """
        assert ChatMigratedError.parse_pair(other_message) is None


# ------------------------------------------------------------------
# telegram_notification: the signal must not be swallowed
# ------------------------------------------------------------------


@pytest.fixture
def notification_service():
    """TelegramNotificationService with a parent whose send_photo we control."""
    parent = Mock()
    parent.bot = AsyncMock()
    parent.media_repo = Mock()
    parent.queue_repo = Mock()
    parent.settings_service = Mock()
    parent.interaction_service = Mock()
    parent.ig_account_service = Mock()
    parent.ig_account_service.count_active_accounts.return_value = 1
    return TelegramNotificationService(parent)


def _wire_send_photo_to_raise(notification_service, exc):
    """Drive send_notification far enough to reach the send, then raise `exc`.

    Everything before the send_photo call is scaffolding, stubbed to the
    shortest path that reaches the real except-block under test. The
    assertions are all about what happens AFTER send_photo raises.

    ``telegram_message_id=None`` is load-bearing, not tidiness. A bare Mock()
    auto-creates that attribute as a truthy Mock, which trips the duplicate-card
    idempotency guard — send_notification then returns True having never
    reached the send at all. Caught while watching the red: the positive
    control returned True instead of False, and the ChatMigrated test was
    "failing" for that reason rather than for the defect. Every test using this
    helper asserts send_photo was actually called, so a future stub that
    short-circuits the path fails loudly instead of passing vacuously.
    """
    svc = notification_service.service
    queue_item = Mock(
        id=uuid4(),
        media_item_id=uuid4(),
        telegram_chat_id=OLD_ID,
        chat_settings_id=uuid4(),
        telegram_message_id=None,
    )
    # Every field the caption builder markdown-escapes must be a real string
    # or None. An auto-Mock is truthy, so it reaches _escape_md and dies on
    # re.sub with "expected string or bytes-like object, got 'Mock'".
    media_item = Mock(
        file_name="x.jpg",
        source_identifier="x.jpg",
        caption="c",
        generated_caption=None,
        category="test",
        title=None,
        link_url=None,
        tags=None,
    )
    svc.queue_repo.get_by_id.return_value = queue_item
    svc.media_repo.get_by_id.return_value = media_item
    svc.ig_account_service.get_active_account.return_value = None
    svc.settings_service.get_settings_by_id.return_value = Mock(
        telegram_chat_id=OLD_ID,
        caption_style="simple",
        enable_instagram_api=False,
        active_instagram_account_id=None,
    )
    svc._is_verbose.return_value = False
    svc.bot.send_photo = AsyncMock(side_effect=exc)
    return queue_item


@pytest.mark.unit
class TestSignalIsNotSwallowed:
    """ChatMigrated must leave send_notification as a typed error, not False."""

    @pytest.mark.asyncio
    async def test_chat_migrated_raises_instead_of_returning_false(
        self, notification_service
    ):
        """The blanket ``except Exception`` must not absorb this one.

        Before the fix this returned False and the id died in a log line.
        """
        _wire_send_photo_to_raise(notification_service, ChatMigrated(NEW_ID))

        with (
            patch(
                "src.services.media_sources.factory.MediaSourceFactory."
                "get_provider_for_media_item"
            ) as provider,
            patch("asyncio.to_thread", new_callable=AsyncMock) as to_thread,
        ):
            provider.return_value = Mock()
            to_thread.return_value = b"bytes"

            with pytest.raises(ChatMigratedError) as caught:
                await notification_service.send_notification(str(uuid4()))

        # The send was genuinely attempted — see the helper's docstring.
        assert notification_service.service.bot.send_photo.called
        assert caught.value.new_chat_id == NEW_ID
        assert caught.value.old_chat_id == OLD_ID

    @pytest.mark.asyncio
    async def test_other_send_failures_still_return_false(self, notification_service):
        """Positive control: the blanket handler is intact for everything else.

        Without this, a change that simply removed the try/except would pass
        the test above while breaking every other failure path.

        A deliberate second copy of the contract asserted by
        ``test_non_auth_error_still_returns_false`` in
        tests/src/services/test_telegram_notification.py — kept local so this
        file states on its own that the new except clause did not widen.
        """
        _wire_send_photo_to_raise(notification_service, RuntimeError("network down"))

        with (
            patch(
                "src.services.media_sources.factory.MediaSourceFactory."
                "get_provider_for_media_item"
            ) as provider,
            patch("asyncio.to_thread", new_callable=AsyncMock) as to_thread,
        ):
            provider.return_value = Mock()
            to_thread.return_value = b"bytes"

            result = await notification_service.send_notification(str(uuid4()))

        assert notification_service.service.bot.send_photo.called
        assert result is False


# ------------------------------------------------------------------
# scheduler: the pair must reach the durable column, without retrying
# ------------------------------------------------------------------


@pytest.fixture
def scheduler_service_mocked():
    """SchedulerService with dependencies mocked (mirrors test_scheduler.py)."""
    with patch.object(SchedulerService, "__init__", lambda self: None):
        service = SchedulerService()
        service.queue_repo = Mock()
        service.history_repo = Mock()
        service.settings_service = Mock()
        service.telegram_service = AsyncMock()
        service.service_name = "SchedulerService"
        service._consecutive_send_failures = 0
        service.track_execution = mock_track_execution
        service.set_result_summary = Mock()
        return service


def _queue_item():
    return Mock(
        id=uuid4(),
        media_item_id=uuid4(),
        chat_settings_id=uuid4(),
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        scheduled_for=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


@pytest.mark.unit
class TestSchedulerRecordsThePair:
    """_send_to_telegram turns the typed error into a durable record."""

    @pytest.mark.asyncio
    async def test_fails_fast_and_records_the_pair(self, scheduler_service_mocked):
        """One attempt, marked failed, and the pair in the durable column.

        One test with several asserts rather than three tests, matching the
        two sibling handler tests it mirrors — ``test_google_drive_auth_error``
        and ``test_ambiguous_delivery_marks_sent_unconfirmed`` in
        tests/src/services/test_scheduler.py — which each cover their whole
        handler contract in one place.

        The three claims:
        - one attempt, not three. A migrated id never un-migrates, so the
          retries are guaranteed failures spent on every tick forever — the
          reasoning that already makes GoogleDriveAuthError fail fast.
        - failing fast must not skip the bookkeeping the retry path does,
          which is the obvious wrong shape of this fix: an early return that
          records nothing and leaves the queue row stuck in 'processing'.
        - the durable column gets the pair, not "send_notification returned
          False".
        """
        service = scheduler_service_mocked
        queue_item = _queue_item()
        service.telegram_service.send_notification = AsyncMock(
            side_effect=ChatMigratedError(OLD_ID, NEW_ID)
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await service._send_to_telegram(queue_item)

        assert result is False
        assert service.telegram_service.send_notification.call_count == 1
        service.queue_repo.update_status.assert_any_call(str(queue_item.id), "failed")
        service.history_repo.create.assert_called_once()
        params = service.history_repo.create.call_args[0][0]
        assert ChatMigratedError.parse_pair(params.error_message) == (OLD_ID, NEW_ID)

    @pytest.mark.asyncio
    async def test_raw_telegram_error_reaches_the_durable_column(
        self, scheduler_service_mocked, notification_service
    ):
        """End to end: a raw telegram ChatMigrated lands as a parseable pair.

        The other tests exercise the two halves separately — the notification
        layer raising, and the scheduler recording — and each half can pass
        while the chain is broken. This is the claim the change actually
        makes: the id Telegram put on the wire reaches a durable column
        instead of dying in a log line.

        Mutation-checked: with the notification catch removed, send_notification
        returns False and this records "send_notification returned False",
        exactly the pre-fix behaviour.
        """
        service = scheduler_service_mocked
        _wire_send_photo_to_raise(notification_service, ChatMigrated(NEW_ID))
        # The real notification method, not a stub standing in for it.
        service.telegram_service.send_notification = (
            notification_service.send_notification
        )

        with (
            patch(
                "src.services.media_sources.factory.MediaSourceFactory."
                "get_provider_for_media_item"
            ) as provider,
            patch("asyncio.to_thread", new_callable=AsyncMock) as to_thread,
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            provider.return_value = Mock()
            to_thread.return_value = b"bytes"

            await service._send_to_telegram(_queue_item())

        assert notification_service.service.bot.send_photo.called
        params = service.history_repo.create.call_args[0][0]
        assert ChatMigratedError.parse_pair(params.error_message) == (OLD_ID, NEW_ID)

    @pytest.mark.asyncio
    async def test_generic_failure_still_retries_three_times(
        self, scheduler_service_mocked
    ):
        """Positive control: fail-fast is scoped to migration, not universal.

        A fix that made every failure fail fast would pass the tests above and
        silently remove retry from real transient outages.

        A deliberate second copy of the retry count asserted by
        ``test_failure_retries_then_marks_failed`` in
        tests/src/services/test_scheduler.py — kept local because the property
        it guards belongs to THIS change, not to the retry loop.
        """
        service = scheduler_service_mocked
        service.telegram_service.send_notification = AsyncMock(return_value=False)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await service._send_to_telegram(_queue_item())

        assert service.telegram_service.send_notification.call_count == 3


# ------------------------------------------------------------------
# Durability: the pair must survive a real round trip through Postgres
# ------------------------------------------------------------------


@pytest.fixture
def history_repo(test_db) -> HistoryRepository:
    """ONE repository bound to the test session, held for the whole test.

    A fixture rather than a per-call helper, for the reason #744 documented:
    BaseRepository closes its session in ``__del__``, so a throwaway repo per
    call lets the garbage collector tear down the session the test is still
    using.
    """
    instance = HistoryRepository.__new__(HistoryRepository)
    instance._db = test_db
    yield instance


def _seed_media_item(session) -> uuid4:
    """A real media_items row, because posting_history FKs to it.

    Found by watching the red: the first draft passed a free-floating UUID and
    hit ForeignKeyViolation. That the constraint fired at all is the point of
    using a real database here.
    """
    from src.models.media_item import MediaItem

    media_item = MediaItem(
        id=uuid4(),
        file_path=f"/tmp/{uuid4()}.jpg",
        file_name="x.jpg",
        file_size=1024,
        file_hash=str(uuid4()),
        source_type="local",
    )
    session.add(media_item)
    session.flush()
    return media_item.id


@pytest.mark.integration
class TestPairIsDurable:
    """Against a REAL database, because 'durable' is the claim under test.

    The tests above assert the right params were CONSTRUCTED. That is not the
    same claim: the whole point of this change is that the pair stops living in
    a log line and lands in a column, and only a real write-then-read proves
    the column holds it. posting_history.error_message is Text and nullable, so
    nothing about that is guaranteed by the type system.
    """

    def test_sweep_finds_migrations_among_other_failures(self, test_db, history_repo):
        """A recovery pass can pick migrations out of a mixed failure corpus.

        This is the shape the still-undecided recovery half will actually use,
        so it is worth proving now that the record supports it — without
        presupposing what recovery then DOES with the pair.

        Deliberately the ONLY database test here. A separate
        write-one-row-and-read-it-back test was dropped as subsumed: this
        assertion cannot pass unless the migration row survived the round trip
        intact, and it additionally proves the discrimination. Subsumption is
        the whole reason — #763 gave each session its own database, so DB tests
        are no longer a contended resource and scarcity is not an argument for
        dropping one.
        """
        media_item_id = _seed_media_item(test_db)
        now = datetime.now(timezone.utc)

        messages = [
            "send_notification returned False",
            ChatMigratedError(OLD_ID, NEW_ID).durable_message(),
            "GoogleDriveAuthError: credentials invalid",
        ]
        queue_item_ids = []
        for message in messages:
            queue_item_id = uuid4()
            queue_item_ids.append(queue_item_id)
            history_repo.create(
                HistoryCreateParams(
                    media_item_id=str(media_item_id),
                    queue_item_id=str(queue_item_id),
                    queue_created_at=now,
                    queue_deleted_at=now,
                    scheduled_for=now,
                    posted_at=now,
                    status="failed",
                    success=False,
                    error_message=message,
                )
            )

        # Scoped to this test's own rows rather than the whole table. Two
        # reasons: repo.create() commits, which releases the test_db savepoint
        # so rows outlive the rollback and leak between tests (the isolation
        # defect #744 documented) — and a real recovery sweep runs against a
        # populated history table anyway, so scoping is the more faithful
        # shape, not a workaround.
        rows = (
            test_db.query(PostingHistory)
            .filter(PostingHistory.queue_item_id.in_(queue_item_ids))
            .all()
        )
        pairs = [
            pair
            for pair in (
                ChatMigratedError.parse_pair(row.error_message) for row in rows
            )
            if pair is not None
        ]

        assert pairs == [(OLD_ID, NEW_ID)]
