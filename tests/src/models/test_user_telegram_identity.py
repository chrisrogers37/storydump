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

    def test_telegram_user_id_is_nullable(self):
        """A user who signed up on the web has no Telegram identity.

        Must agree with `proposed/relax_telegram_identity_not_null.sql`: a model
        still declaring `nullable=False` would make that DDL cosmetic, because
        the ORM would keep refusing the insert the database now permits.
        """
        assert User.telegram_user_id.nullable is True

    def test_telegram_user_id_is_still_unique(self):
        """NULLS DISTINCT lets many web-only users coexist while a real
        Telegram id still collides."""
        assert User.telegram_user_id.unique is True
