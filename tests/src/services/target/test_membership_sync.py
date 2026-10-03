"""The `06` Telegram join path, unit tier (#1242): who is observed, what the
door is asked, and that every outcome is named and silent."""

from __future__ import annotations

import pytest

from src.services.target import membership_sync


def _update(chat_type="supergroup", external_ref=-100777, sender=42, text="hello"):
    return {
        "message": {
            "text": text,
            "chat": {"id": external_ref, "type": chat_type},
            "from": {"id": sender},
        }
    }


class TestWhatCountsAsAGroupMessage:
    def test_a_person_speaking_in_a_group(self):
        assert membership_sync.group_message_of(_update()) == (
            "supergroup",
            "-100777",
            "42",
        )

    def test_a_dm_a_channel_post_or_no_sender_is_none(self):
        assert membership_sync.group_message_of(_update(chat_type="private")) is None
        assert (
            membership_sync.group_message_of(
                {"channel_post": {"chat": {"id": 1, "type": "channel"}}}
            )
            is None
        )
        assert (
            membership_sync.group_message_of(
                {"message": {"chat": {"id": 1, "type": "group"}}}
            )
            is None
        )


class TestWhoElseTheBotCanSee:
    def test_bots_are_never_observed(self):
        update = _update()
        update["message"]["from"]["is_bot"] = True
        assert membership_sync.group_members_of(update) == []

    def test_people_added_by_a_service_message_count_too(self):
        update = _update()
        update["message"]["new_chat_members"] = [
            {"id": 8},
            {"id": 42},
            {"id": 9, "is_bot": True},
        ]
        assert membership_sync.group_members_of(update) == [
            ("supergroup", "-100777", "42"),
            ("supergroup", "-100777", "8"),
        ]


class _Conn:
    def __init__(self, row):
        self.row, self.statements = row, []

    async def execute(self, statement, params=None):
        self.statements.append((str(statement), params))
        row = self.row

        class _R:
            def first(self_inner):
                return row

        return _R()


@pytest.fixture()
def linked(monkeypatch):
    holder = {"user": "u-1"}

    async def user_for_identity(executor, *, provider, external_id):
        holder["asked"] = (provider, external_id)
        return holder["user"]

    monkeypatch.setattr(
        membership_sync.identity, "user_for_identity", user_for_identity
    )

    async def row(executor, sql, **params):
        assert "FROM users" in sql
        return {"state": holder.get("state", "active")}

    monkeypatch.setattr(membership_sync.readers, "row", row)
    return holder


class TestObserve:
    async def test_a_linked_person_in_a_bound_group_joins_through_the_door(
        self, linked
    ):
        conn = _Conn(("ws-1", "joined"))
        result = await membership_sync.observe(
            conn, chat_type="supergroup", external_ref="-100777", telegram_user_id="42"
        )
        assert result.outcome == "joined" and result.handled and result.reply is None
        assert linked["asked"] == ("telegram", "42")
        ((sql, params),) = conn.statements
        assert "fn_group_member_seen" in sql
        assert params == {"ch": "telegram_group", "ref": "-100777", "u": "u-1"}

    async def test_an_unknown_identity_asks_the_door_nothing(self, linked):
        linked["user"] = None
        conn = _Conn(None)
        result = await membership_sync.observe(
            conn, chat_type="group", external_ref="-5", telegram_user_id="7"
        )
        assert result.outcome == "unknown_identity" and not result.handled
        assert conn.statements == []

    @pytest.mark.parametrize(
        "door_outcome",
        [
            "already_member",
            "unbound_chat",
            "revoked_chat",
            "workspace_inactive",
            "unknown_user",
        ],
    )
    async def test_the_doors_other_answers_are_named_and_silent(
        self, linked, door_outcome
    ):
        conn = _Conn(("ws-1", door_outcome))
        result = await membership_sync.observe(
            conn, chat_type="group", external_ref="-5", telegram_user_id="42"
        )
        assert (
            result.outcome == door_outcome
            and not result.handled
            and result.reply is None
        )


class TestADisabledAccountIsRefusedByTheCaller:
    async def test_no_membership_for_a_disabled_user(self, linked):
        linked["state"] = "disabled"
        conn = _Conn(None)
        result = await membership_sync.observe(
            conn, chat_type="group", external_ref="-5", telegram_user_id="42"
        )
        assert result.outcome == "user_inactive" and not result.handled
        assert conn.statements == [], "the door is never asked"
