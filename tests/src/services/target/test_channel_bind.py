"""The `bind-` lane of the `/start` door (owner ruling 2026-09-05, #1175 D-3/D-4):
an admin mints a one-shot `startgroup` link from Settings; whoever opens it
picks a group, Telegram adds the bot and sends `/start bind-<state>` there,
and the door binds THAT chat to the pinned workspace. Refusals are silent
(the router's existence-oracle rule); a bound group is told so."""

from __future__ import annotations

import pytest

from src.services.target import bindings, channel_bind
from src.services.target.start_router import StartContext, StartRouter


def ctx(payload="STATE1", chat_id="-100777", chat_type="supergroup", uid="tg-42"):
    return StartContext(
        payload=payload,
        telegram_user_id=uid,
        chat_id=chat_id,
        chat_type=chat_type,
        display_name="ada",
    )


class _Fake:
    def __init__(self):
        self.state_row = {"user_id": "u1", "workspace_id": "ws-1"}
        self.refuse = None
        self.bind_outcome = bindings.BOUND
        self.bound = []
        self.name = "Northside Coffee"


@pytest.fixture()
def patched(monkeypatch):
    f = _Fake()

    async def consume(conn, **kw):
        f.consume_kw = kw
        if f.refuse:
            raise channel_bind.ig_login_oauth.OAuthStateRefused(f.refuse)
        return f.state_row

    async def bind(session, **kw):
        f.bound.append(kw)
        return f.bind_outcome

    async def row(executor, sql, **params):
        return {"name": f.name}

    monkeypatch.setattr(channel_bind.ig_login_oauth, "consume_state", consume)
    monkeypatch.setattr(channel_bind.bindings, "bind", bind)
    monkeypatch.setattr(channel_bind.readers, "row", row)
    return f


class TestTheHappyPath:
    async def test_a_valid_state_binds_the_group_to_the_pinned_workspace(self, patched):
        result = await channel_bind.handle_bind(object(), ctx())
        assert result.handled and result.outcome == "bound"
        assert patched.bound == [
            {
                "workspace_id": "ws-1",
                "chat_type": "supergroup",
                "external_ref": "-100777",
            }
        ]
        assert "Northside Coffee" in result.reply

    async def test_the_state_is_consumed_pinned_to_purpose_AND_provider(self, patched):
        await channel_bind.handle_bind(object(), ctx())
        assert patched.consume_kw["expected_purpose"] == "bind"
        assert patched.consume_kw["expected_provider"] == "telegram"

    async def test_rebinding_a_group_this_workspace_already_held_is_fine(self, patched):
        patched.bind_outcome = bindings.REBOUND
        result = await channel_bind.handle_bind(object(), ctx())
        assert result.handled and result.outcome == "rebound"


class TestEveryRefusalIsNamedAndSilent:
    async def test_a_refused_state_yields_no_reply_text(self, patched):
        patched.refuse = "consumed"
        result = await channel_bind.handle_bind(object(), ctx())
        assert not result.handled and result.reply is None
        assert result.outcome == "state_refused"
        assert patched.bound == []

    async def test_a_private_chat_cannot_be_bound_and_the_state_is_still_spent(
        self, patched
    ):
        result = await channel_bind.handle_bind(object(), ctx(chat_type="private"))
        assert not result.handled and result.outcome == "not_a_group"
        assert patched.bound == []

    async def test_a_group_another_workspace_holds_is_refused_by_name(self, patched):
        patched.bind_outcome = bindings.TAKEN
        result = await channel_bind.handle_bind(object(), ctx())
        assert not result.handled and result.outcome == "taken" and result.reply is None

    async def test_a_state_without_a_workspace_is_refused_not_trusted(self, patched):
        patched.state_row = {"user_id": "u1", "workspace_id": None}
        result = await channel_bind.handle_bind(object(), ctx())
        assert not result.handled and result.outcome == "state_without_workspace"


class TestTheDeepLinkAndRegistration:
    def test_the_deep_link_opens_the_group_picker_with_the_disjoint_prefix(self):
        link = channel_bind.deep_link("storydump_app_bot", "st4te")
        assert link == "https://t.me/storydump_app_bot?startgroup=bind-st4te"

    def test_registering_puts_it_behind_the_bind_prefix_beside_link(self):
        from src.services.target import identity_link

        router = StartRouter()
        identity_link.register(router)
        channel_bind.register(router)
        assert set(router._handlers) == {"link-", "bind-"}

    def test_the_shared_door_serves_it(self):
        from src.services.target.telegram_dispatch import build_router

        assert "bind-" in build_router()._handlers


class TestIssuingRetiresTheWorkspacesEarlierLinks:
    async def test_one_live_bind_link_per_workspace(self, monkeypatch):
        seen = []

        class _Conn:
            async def execute(self, statement, params=None):
                seen.append((str(statement), params))

        async def issue_state(conn, **kw):
            seen.append(("issue", kw))
            return "st4te"

        monkeypatch.setattr(channel_bind.ig_login_oauth, "issue_state", issue_state)
        link = await channel_bind.issue_bind_state(
            _Conn(), user_id="u1", workspace_id="ws-1", bot_username="storydump_app_bot"
        )
        assert link == "https://t.me/storydump_app_bot?startgroup=bind-st4te"
        retire, issue = seen
        assert "UPDATE oauth_states SET consumed_at = now()" in retire[0]
        assert retire[1] == {"purpose": "bind", "provider": "telegram", "ws": "ws-1"}
        assert issue[1] == {
            "purpose": "bind",
            "provider": "telegram",
            "user_id": "u1",
            "workspace_id": "ws-1",
        }
