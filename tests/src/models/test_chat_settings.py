"""Tests for ChatSettings model definition."""

import pytest

from src.models.chat_settings import ChatSettings


@pytest.mark.unit
class TestChatSettingsModel:
    """Tests for ChatSettings model column definitions and defaults."""

    def test_tablename(self):
        assert ChatSettings.__tablename__ == "chat_settings"

    def test_id_default_generates_uuids(self):
        default_fn = ChatSettings.id.default.arg
        assert callable(default_fn)
        assert default_fn.__name__ == "uuid4"

    def test_telegram_chat_id_is_nullable(self):
        """RE-POINTED, not deleted (#1015).

        This asserted `nullable is False` and was correct until the binding
        stopped being the tenant's identity. A workspace created through web
        signup has no Telegram chat, so the column is now an OPTIONAL BINDING
        ATTRIBUTE and `id` carries the identity it always carried.

        The assertion is inverted rather than removed because the fact is still
        worth pinning — in the other direction. Restoring `NOT NULL` would make
        every web workspace unrepresentable, and this is what would say so.

        It also has to agree with the DDL: `proposed/
        relax_telegram_identity_not_null.sql` drops the constraint, and a model
        still declaring `nullable=False` would make that DDL cosmetic (the ORM
        would keep refusing the insert the database now permits).
        """
        assert ChatSettings.telegram_chat_id.nullable is True

    def test_telegram_chat_id_is_still_unique(self):
        """UNIQUE survives the nullability change, and must.

        PostgreSQL UNIQUE is NULLS DISTINCT, so many unbound workspaces coexist
        while a real chat id still collides — which is the only reason dropping
        NOT NULL is safe here rather than merely convenient.
        """
        assert ChatSettings.telegram_chat_id.unique is True

    def test_dry_run_mode_defaults_to_false(self):
        assert ChatSettings.dry_run_mode.default.arg is False

    def test_enable_instagram_api_defaults_to_false(self):
        assert ChatSettings.enable_instagram_api.default.arg is False

    def test_is_paused_defaults_to_false(self):
        assert ChatSettings.is_paused.default.arg is False

    def test_posts_per_day_defaults_to_three(self):
        assert ChatSettings.posts_per_day.default.arg == 3

    def test_posting_hours_start_defaults_to_14(self):
        assert ChatSettings.posting_hours_start.default.arg == 14

    def test_posting_hours_end_defaults_to_2(self):
        assert ChatSettings.posting_hours_end.default.arg == 2

    def test_show_verbose_notifications_defaults_to_true(self):
        assert ChatSettings.show_verbose_notifications.default.arg is True

    def test_media_sync_enabled_defaults_to_false(self):
        assert ChatSettings.media_sync_enabled.default.arg is False

    def test_active_instagram_account_id_nullable(self):
        assert ChatSettings.active_instagram_account_id.nullable is True

    def test_repr_format(self):
        item = ChatSettings(telegram_chat_id=-1001234567, is_paused=False)
        result = repr(item)
        assert "-1001234567" in result
        assert "False" in result
