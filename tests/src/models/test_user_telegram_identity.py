"""#1015 — `users.telegram_user_id` is an optional provider id, not identity.

The twin of `test_chat_settings.py`'s binding assertions. `users` had no
nullability test at all, so the model/DDL lockstep that matters for web signup
was pinned on one of the two columns and not the other.
"""

import pytest

from src.models.user import User


@pytest.mark.unit
class TestUserTelegramIdentity:
    def test_id_is_the_primary_key(self):
        """The fact the whole web-signup design rests on: `id` is a UUID PK,
        and `telegram_user_id` never was the key."""
        assert User.id.primary_key is True
        assert User.telegram_user_id.primary_key is False

    def test_telegram_user_id_is_not_nullable_yet(self):
        """The other half of the #1015 lockstep tripwire.

        `users` had no nullability test at all, so the model/DDL pairing that
        matters for web signup was pinned on one of the two columns and not the
        other. It is pinned on both now.

        When `proposed/relax_telegram_identity_not_null.sql` is numbered and
        lands, this flips WITH the model in that PR. Flipping the model first
        reddens `TestSchemaParity`, which replays the corpus and diffs it
        against these models — and leaving the model behind afterwards would
        make the DDL cosmetic, since the ORM would keep refusing the insert the
        database now permits. Both directions are covered.
        """
        assert User.telegram_user_id.nullable is False

    def test_telegram_user_id_is_still_unique(self):
        """NULLS DISTINCT lets many web-only users coexist while a real
        Telegram id still collides."""
        assert User.telegram_user_id.unique is True
