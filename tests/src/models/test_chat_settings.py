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

    def test_telegram_chat_id_is_not_nullable_yet(self):
        """The LOCKSTEP tripwire for #1015, not a statement that this is right.

        Web signup needs this nullable — `chat_settings.id` is the tenant
        identity and this is an optional binding. The DDL that drops it is
        written but UNNUMBERED (`scripts/migrations/proposed/`), because the
        legacy lineage has no free slot; see that file's header.

        The model must not move ahead of the corpus. `TestSchemaParity` replays
        the migrations and diffs them against these models, so flipping the
        model alone reddens it — measured, that is how this pairing was found.
        When the migration is numbered and lands, this assertion and the model
        flip in the same PR, and so does its twin in
        `test_user_telegram_identity.py`.
        """
        assert ChatSettings.telegram_chat_id.nullable is False

    def test_telegram_chat_id_is_unique(self):
        """UNIQUE must survive the eventual nullability change.

        PostgreSQL UNIQUE is NULLS DISTINCT, so many unbound workspaces will
        coexist while a real chat id still collides — which is the only reason
        dropping NOT NULL is safe here rather than merely convenient.
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
