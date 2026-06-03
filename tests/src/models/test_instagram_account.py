"""Tests for InstagramAccount model definition."""

import pytest

from src.models.instagram_account import InstagramAccount


@pytest.mark.unit
class TestInstagramAccountModel:
    """Tests for InstagramAccount model column definitions and defaults."""

    def test_tablename(self):
        assert InstagramAccount.__tablename__ == "instagram_accounts"

    def test_id_default_generates_uuids(self):
        default_fn = InstagramAccount.id.default.arg
        assert callable(default_fn)
        assert default_fn.__name__ == "uuid4"

    def test_display_name_not_nullable(self):
        assert InstagramAccount.display_name.nullable is False

    def test_instagram_account_id_not_nullable(self):
        assert InstagramAccount.instagram_account_id.nullable is False

    def test_instagram_account_id_is_unique(self):
        assert InstagramAccount.instagram_account_id.unique is True

    def test_is_active_defaults_to_true(self):
        assert InstagramAccount.is_active.default.arg is True

    def test_auth_method_column_removed(self):
        """Credential refactor PR 5 (#468) — auth_method moved to
        api_tokens.auth_method (migration 041 drops the legacy column
        on instagram_accounts). The mapper must no longer expose it."""
        assert not hasattr(InstagramAccount, "auth_method") or (
            "auth_method" not in InstagramAccount.__table__.columns
        )

    def test_repr_format(self):
        item = InstagramAccount(display_name="My Brand", instagram_username="mybrand")
        result = repr(item)
        assert "My Brand" in result
        assert "@mybrand" in result

    def test_repr_with_none_username(self):
        item = InstagramAccount(display_name="My Brand", instagram_username=None)
        result = repr(item)
        assert "My Brand" in result
