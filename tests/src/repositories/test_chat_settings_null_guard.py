"""#1015 — the null-chat-id guard on ChatSettingsRepository.get_by_chat_id.

WHAT THIS PINS AND WHY IT IS NOT OBVIOUS. Migration `proposed/
relax_telegram_identity_not_null.sql` drops `NOT NULL` from
`chat_settings.telegram_chat_id`, so rows with a null binding start to exist.
The guard refuses a null INPUT. The argument for deleting it is that `= NULL`
matches nothing in SQL — true of raw SQL, and false here, because the ORM emits
`IS NULL` instead. `test_the_orm_emits_is_null_not_equals_null` pins that
premise so the guard's reason is checkable rather than remembered.

`test_a_null_chat_id_does_not_return_another_tenants_row` is the guard's own
test and is written to FAIL LOUDLY if the guard is deleted: the mocked session
is armed to return a foreign tenant, exactly as a real session would once
unbound rows exist, so removing the guard returns that row instead of raising.
A test that merely asserted `raises(ValueError)` against a bare Mock would still
fail on deletion, but for the wrong reason — it would not show what the defect
costs.
"""

import pytest
from unittest.mock import MagicMock, patch

from sqlalchemy.dialects import postgresql

from src.models.chat_settings import ChatSettings
from src.repositories.chat_settings_repository import ChatSettingsRepository


@pytest.mark.unit
class TestNullChatIdGuard:
    @pytest.fixture
    def foreign_tenant(self):
        """A row belonging to somebody else — what a null input would find."""
        other = MagicMock(spec=ChatSettings)
        other.display_name = "another tenant's workspace"
        return other

    @pytest.fixture
    def mock_db(self, foreign_tenant):
        db = MagicMock()
        # A real session, post-migration, answers `IS NULL` with an unbound row.
        db.query.return_value.filter.return_value.first.return_value = foreign_tenant
        return db

    @pytest.fixture
    def repo(self, mock_db):
        with patch("src.repositories.base_repository.get_db") as get_db:
            get_db.return_value = iter([mock_db])
            repo = ChatSettingsRepository()
            repo._db = mock_db
            return repo

    def test_a_null_chat_id_does_not_return_another_tenants_row(
        self, repo, mock_db, foreign_tenant
    ):
        with pytest.raises(ValueError) as exc:
            repo.get_by_chat_id(None)
        assert "null telegram_chat_id" in str(exc.value)
        # The guard must intercept BEFORE the query, not filter its result:
        # reaching the session at all means a foreign row was fetched.
        mock_db.query.assert_not_called()

    def test_a_real_chat_id_still_resolves(self, repo, foreign_tenant):
        """The positive control — the guard must not refuse legitimate input."""
        assert repo.get_by_chat_id(-1001234567890) is foreign_tenant

    def test_require_by_chat_id_cannot_catch_it_on_the_result(self, repo):
        """Why the check has to be on the input.

        `require_by_chat_id` raises only when the RESULT is None. With a row
        found — which is what a null input finds once unbound rows exist — its
        refusal never fires. It must therefore surface the guard's ValueError
        rather than a tenancy refusal.
        """
        with pytest.raises(ValueError):
            repo.require_by_chat_id(None)

    def test_the_orm_emits_is_null_not_equals_null(self):
        """Pins the guard's premise against the raw-SQL counter-argument."""
        compiled = str(
            (ChatSettings.telegram_chat_id == None).compile(  # noqa: E711
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        assert "IS NULL" in compiled, (
            "SQLAlchemy no longer compiles `== None` to `IS NULL`; the guard's "
            "stated reason is stale even if the guard is still correct"
        )
        assert "= NULL" not in compiled
