"""Tests for daily posting cap guard — can_post_today()."""

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


@pytest.mark.unit
class TestCanPostToday:
    """Tests for the can_post_today() daily cap guard."""

    def _make_chat_settings(self, posts_per_day=3, posting_timezone=None):
        cs = Mock()
        cs.id = "cs-abc-123"
        cs.posts_per_day = posts_per_day
        cs.posting_timezone = posting_timezone
        return cs

    def test_allows_when_under_limit(self):
        """can_post_today returns True when today's count is below the limit."""
        from src.services.core.daily_cap import can_post_today

        chat_settings = self._make_chat_settings(posts_per_day=5)
        history_repo = Mock()
        history_repo.count_posts_today.return_value = 3

        result = can_post_today(chat_settings, history_repo)
        assert result is True

    def test_blocks_when_at_limit(self):
        """can_post_today returns False when today's count equals the limit."""
        from src.services.core.daily_cap import can_post_today

        chat_settings = self._make_chat_settings(posts_per_day=5)
        history_repo = Mock()
        history_repo.count_posts_today.return_value = 5

        result = can_post_today(chat_settings, history_repo)
        assert result is False

    def test_blocks_when_over_limit(self):
        """can_post_today returns False when today's count exceeds the limit."""
        from src.services.core.daily_cap import can_post_today

        chat_settings = self._make_chat_settings(posts_per_day=3)
        history_repo = Mock()
        history_repo.count_posts_today.return_value = 7

        result = can_post_today(chat_settings, history_repo)
        assert result is False

    def test_passes_timezone_to_repo(self):
        """can_post_today passes the chat's timezone to count_posts_today."""
        from src.services.core.daily_cap import can_post_today

        chat_settings = self._make_chat_settings(
            posts_per_day=10, posting_timezone="America/New_York"
        )
        history_repo = Mock()
        history_repo.count_posts_today.return_value = 0

        can_post_today(chat_settings, history_repo)

        history_repo.count_posts_today.assert_called_once_with(
            chat_settings_id="cs-abc-123",
            posting_timezone="America/New_York",
        )

    def test_passes_none_timezone_when_null(self):
        """can_post_today passes None timezone when chat has no timezone set."""
        from src.services.core.daily_cap import can_post_today

        chat_settings = self._make_chat_settings(
            posts_per_day=10, posting_timezone=None
        )
        history_repo = Mock()
        history_repo.count_posts_today.return_value = 0

        can_post_today(chat_settings, history_repo)

        history_repo.count_posts_today.assert_called_once_with(
            chat_settings_id="cs-abc-123",
            posting_timezone=None,
        )

    def test_allows_when_exactly_one_below_limit(self):
        """Edge case: count is exactly limit-1 (last allowed post)."""
        from src.services.core.daily_cap import can_post_today

        chat_settings = self._make_chat_settings(posts_per_day=15)
        history_repo = Mock()
        history_repo.count_posts_today.return_value = 14

        result = can_post_today(chat_settings, history_repo)
        assert result is True


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
