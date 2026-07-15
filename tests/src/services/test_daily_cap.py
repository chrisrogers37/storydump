"""Tests for daily posting cap guard — can_post_today()."""

from datetime import datetime, timedelta

import pytest
from unittest.mock import Mock, MagicMock, patch


@pytest.mark.unit
class TestCountPostsToday:
    """Tests for HistoryRepository.count_posts_today()."""

    @pytest.fixture
    def mock_db(self):
        return MagicMock()

    @pytest.fixture
    def history_repo(self, mock_db):
        with patch("src.repositories.base_repository.get_db") as mock_get_db:
            mock_get_db.return_value = iter([mock_db])
            from src.repositories.history_repository import HistoryRepository

            repo = HistoryRepository()
            repo._db = mock_db
            return repo

    def _setup_count_query(self, mock_db, count):
        """Set up the mock chain for a count query returning `count`."""
        mock_db.query.return_value.filter.return_value.with_entities.return_value.filter.return_value.scalar.return_value = count
        # Also handle the _tenant_query -> with_entities -> filter -> scalar chain
        (
            mock_db.query.return_value.filter.return_value.with_entities.return_value.filter.return_value.scalar.return_value  # _tenant_query
        ) = count

    def test_returns_zero_when_no_posts(self, history_repo, mock_db):
        """count_posts_today returns 0 when no posts exist for today."""
        self._setup_count_query(mock_db, 0)
        result = history_repo.count_posts_today(chat_settings_id="abc-123")
        assert result == 0

    def test_returns_count_when_posts_exist(self, history_repo, mock_db):
        """count_posts_today returns the actual count of today's posts."""
        self._setup_count_query(mock_db, 5)
        result = history_repo.count_posts_today(chat_settings_id="abc-123")
        assert result == 5

    def test_returns_zero_for_null_scalar(self, history_repo, mock_db):
        """count_posts_today returns 0 when scalar returns None."""
        self._setup_count_query(mock_db, None)
        result = history_repo.count_posts_today(chat_settings_id="abc-123")
        assert result == 0


def _make_chat_settings(posts_per_day=3, posting_timezone=None):
    cs = Mock()
    cs.id = "cs-abc-123"
    cs.posts_per_day = posts_per_day
    cs.posting_timezone = posting_timezone
    return cs


def _make_queue_repo(publishing=0):
    """Mock queue repo whose count_recent_by_status(['publishing'], since=...)
    returns `publishing` — the time-bounded in-flight count the cap uses."""
    queue_repo = Mock()
    queue_repo.count_recent_by_status.return_value = publishing
    return queue_repo


@pytest.mark.unit
class TestCanPostToday:
    """Tests for the can_post_today() daily cap guard."""

    def test_allows_when_under_limit(self):
        """can_post_today returns True when today's count is below the limit."""
        from src.services.core.daily_cap import can_post_today

        chat_settings = _make_chat_settings(posts_per_day=5)
        history_repo = Mock()
        history_repo.count_posts_today.return_value = 3

        result = can_post_today(chat_settings, history_repo, _make_queue_repo())
        assert result is True

    def test_blocks_when_at_limit(self):
        """can_post_today returns False when today's count equals the limit."""
        from src.services.core.daily_cap import can_post_today

        chat_settings = _make_chat_settings(posts_per_day=5)
        history_repo = Mock()
        history_repo.count_posts_today.return_value = 5

        result = can_post_today(chat_settings, history_repo, _make_queue_repo())
        assert result is False

    def test_blocks_when_over_limit(self):
        """can_post_today returns False when today's count exceeds the limit."""
        from src.services.core.daily_cap import can_post_today

        chat_settings = _make_chat_settings(posts_per_day=3)
        history_repo = Mock()
        history_repo.count_posts_today.return_value = 7

        result = can_post_today(chat_settings, history_repo, _make_queue_repo())
        assert result is False

    def test_passes_timezone_to_repo(self):
        """can_post_today passes the chat's timezone to count_posts_today."""
        from src.services.core.daily_cap import can_post_today

        chat_settings = _make_chat_settings(
            posts_per_day=10, posting_timezone="America/New_York"
        )
        history_repo = Mock()
        history_repo.count_posts_today.return_value = 0

        can_post_today(chat_settings, history_repo, _make_queue_repo())

        history_repo.count_posts_today.assert_called_once_with(
            chat_settings_id="cs-abc-123",
            posting_timezone="America/New_York",
        )

    def test_passes_none_timezone_when_null(self):
        """can_post_today passes None timezone when chat has no timezone set."""
        from src.services.core.daily_cap import can_post_today

        chat_settings = _make_chat_settings(posts_per_day=10, posting_timezone=None)
        history_repo = Mock()
        history_repo.count_posts_today.return_value = 0

        can_post_today(chat_settings, history_repo, _make_queue_repo())

        history_repo.count_posts_today.assert_called_once_with(
            chat_settings_id="cs-abc-123",
            posting_timezone=None,
        )

    def test_allows_when_exactly_one_below_limit(self):
        """Edge case: count is exactly limit-1 (last allowed post)."""
        from src.services.core.daily_cap import can_post_today

        chat_settings = _make_chat_settings(posts_per_day=15)
        history_repo = Mock()
        history_repo.count_posts_today.return_value = 14

        result = can_post_today(chat_settings, history_repo, _make_queue_repo())
        assert result is True


@pytest.mark.unit
class TestCanPostTodayCountsPublishing:
    """A 'publishing' queue row (a claimed, possibly-published story) counts
    toward the daily cap, so a crash mid-publish is counted exactly once (#549)."""

    def test_crashed_mid_publish_story_counts_once(self):
        """A crashed publish leaves a 'publishing' row and NO history row.

        can_post_today must still count it, so the crashed story consumes its
        slot and an over-cap post can't slip through while it's unconfirmed.
        """
        from src.services.core.daily_cap import can_post_today

        chat_settings = _make_chat_settings(posts_per_day=1)
        history_repo = Mock()
        history_repo.count_posts_today.return_value = 0  # nothing recorded yet
        queue_repo = _make_queue_repo(publishing=1)  # one in-flight/crashed publish

        result = can_post_today(chat_settings, history_repo, queue_repo)

        assert result is False  # 0 history + 1 publishing >= cap of 1
        # Only *recent* publishing rows tax the cap — a time bound so a stuck
        # row can't wedge the cap forever (rajan #564).
        assert queue_repo.count_recent_by_status.call_count == 1
        call = queue_repo.count_recent_by_status.call_args
        assert call.args[0] == ["publishing"]
        assert call.kwargs["chat_settings_id"] == "cs-abc-123"
        assert "since" in call.kwargs

    def test_no_double_count_after_finalize(self):
        """After the atomic finalize (history written, publishing row deleted)
        the story is counted once via history — not twice."""
        from src.services.core.daily_cap import can_post_today

        chat_settings = _make_chat_settings(posts_per_day=2)
        history_repo = Mock()
        history_repo.count_posts_today.return_value = 1  # finalized
        queue_repo = _make_queue_repo(publishing=0)  # row deleted by finalize

        # 1 history + 0 publishing = 1 < cap of 2 → still allowed, counted once
        assert can_post_today(chat_settings, history_repo, queue_repo) is True

    def test_history_plus_publishing_sum_reaches_cap(self):
        """posted-today + in-flight publishing together reach the cap."""
        from src.services.core.daily_cap import can_post_today

        chat_settings = _make_chat_settings(posts_per_day=3)
        history_repo = Mock()
        history_repo.count_posts_today.return_value = 2
        queue_repo = _make_queue_repo(publishing=1)

        # 2 + 1 = 3 >= 3 → blocked
        assert can_post_today(chat_settings, history_repo, queue_repo) is False


class _FakeQueueRepoWithRows:
    """Queue-repo stand-in that applies count_recent_by_status's created_at
    bound against in-memory rows, so the time bound is exercised for real
    (no DB, no sleep — created_at is set relative to now)."""

    def __init__(self, rows):
        self._rows = rows  # Mock(status=..., created_at=datetime)

    def count_recent_by_status(self, statuses, since, chat_settings_id=None):
        return sum(
            1 for r in self._rows if r.status in statuses and r.created_at >= since
        )


@pytest.mark.unit
class TestCanPostTodayTimeBoundsPublishing:
    """The 'publishing' cap count is time-bounded: a fresh in-flight publish
    still consumes a slot, but a stale (presumed-stuck) publishing row must NOT
    tax the cap forever — otherwise a handful of stuck rows silently wedge a
    chat's auto-posting with no recovery (rajan #564 finding 2)."""

    def test_fresh_publishing_row_counts_toward_cap(self):
        """A publishing row younger than the bound is a live publish → counted,
        so an over-cap post can't slip through while it's genuinely in flight."""
        from src.services.core.daily_cap import can_post_today

        chat_settings = _make_chat_settings(posts_per_day=1)
        history_repo = Mock()
        history_repo.count_posts_today.return_value = 0
        fresh = Mock(status="publishing", created_at=datetime.utcnow())
        queue_repo = _FakeQueueRepoWithRows([fresh])

        # 0 history + 1 fresh publishing >= cap of 1 → blocked.
        assert can_post_today(chat_settings, history_repo, queue_repo) is False

    def test_stale_publishing_rows_do_not_wedge_cap(self):
        """N stale publishing rows (older than the bound) must NOT consume the
        cap — a posts_per_day=3 chat with 3 stuck rows can still post."""
        from src.services.core.daily_cap import can_post_today

        chat_settings = _make_chat_settings(posts_per_day=3)
        history_repo = Mock()
        history_repo.count_posts_today.return_value = 0
        stale = [
            Mock(
                status="publishing",
                created_at=datetime.utcnow() - timedelta(minutes=30),
            )
            for _ in range(3)
        ]
        queue_repo = _FakeQueueRepoWithRows(stale)

        # All 3 are stale → excluded → 0 used < cap of 3 → still allowed.
        assert can_post_today(chat_settings, history_repo, queue_repo) is True


@pytest.mark.unit
class TestCountPostsTodayTimezone:
    """Tests for count_posts_today timezone boundary handling."""

    @pytest.fixture
    def mock_db(self):
        return MagicMock()

    @pytest.fixture
    def history_repo(self, mock_db):
        with patch("src.repositories.base_repository.get_db") as mock_get_db:
            mock_get_db.return_value = iter([mock_db])
            from src.repositories.history_repository import HistoryRepository

            repo = HistoryRepository()
            repo._db = mock_db
            return repo

    def test_uses_utc_when_no_timezone(self, history_repo, mock_db):
        """When posting_timezone is None, day boundary is computed in UTC."""
        # Just verify it doesn't crash and calls through
        (
            mock_db.query.return_value.filter.return_value.with_entities.return_value.filter.return_value.scalar.return_value
        ) = 3
        result = history_repo.count_posts_today(
            chat_settings_id="abc", posting_timezone=None
        )
        assert result == 3

    def test_uses_provided_timezone(self, history_repo, mock_db):
        """When posting_timezone is provided, day boundary uses that timezone."""
        (
            mock_db.query.return_value.filter.return_value.with_entities.return_value.filter.return_value.scalar.return_value
        ) = 7
        result = history_repo.count_posts_today(
            chat_settings_id="abc", posting_timezone="America/New_York"
        )
        assert result == 7

    def test_falls_back_to_utc_on_invalid_timezone(self, history_repo, mock_db):
        """Invalid timezone falls back to UTC without crashing."""
        (
            mock_db.query.return_value.filter.return_value.with_entities.return_value.filter.return_value.scalar.return_value
        ) = 2
        result = history_repo.count_posts_today(
            chat_settings_id="abc", posting_timezone="Invalid/Timezone"
        )
        assert result == 2
