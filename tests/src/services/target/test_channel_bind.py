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
        self.tapper_user = "u1"
        self.gucs = []


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

    async def user_for_identity(executor, *, provider, external_id):
        f.identity_kw = {"provider": provider, "external_id": external_id}
        return f.tapper_user

    async def apply_gucs(executor, **kw):
        f.gucs.append(kw)

    monkeypatch.setattr(channel_bind.ig_login_oauth, "consume_state", consume)
    monkeypatch.setattr(channel_bind.bindings, "bind", bind)
    monkeypatch.setattr(channel_bind.readers, "row", row)
    monkeypatch.setattr(channel_bind.identity, "user_for_identity", user_for_identity)
    monkeypatch.setattr(channel_bind.unit_of_work, "apply_gucs", apply_gucs)
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
        assert patched.consume_kw["state"] == "STATE1"
        assert patched.consume_kw["expected_purpose"] == "bind"
        assert patched.consume_kw["expected_provider"] == "telegram"

    async def test_the_tapper_is_checked_against_the_minter_by_linked_identity(
        self, patched
    ):
        await channel_bind.handle_bind(object(), ctx(uid="tg-42"))
        assert patched.identity_kw == {"provider": "telegram", "external_id": "tg-42"}

    async def test_the_write_runs_under_the_states_tenant_and_actor(self, patched):
        """The door's connection carries no context; the consumed state is
        the pre-context path (#1240 review). Set before anything is written."""
        await channel_bind.handle_bind(object(), ctx())
        assert patched.gucs == [
            {
                "tenant_id": "ws-1",
                "actor_kind": "user",
                "actor_user_id": "u1",
                "channel": "telegram",
            }
        ]

    async def test_rebinding_a_group_this_workspace_already_held_is_fine(self, patched):
        patched.bind_outcome = bindings.REBOUND
        result = await channel_bind.handle_bind(object(), ctx())
        assert result.handled and result.outcome == "rebound"


class TestRefusalsBeforeTheTapperIsProvenAreSilent:
    async def test_a_refused_state_yields_no_reply_text(self, patched):
        patched.refuse = "consumed"
        result = await channel_bind.handle_bind(object(), ctx())
        assert not result.handled and result.reply is None
        assert result.outcome == "state_refused"
        assert patched.bound == [] and patched.gucs == []

    async def test_a_forwarded_link_binds_nothing(self, patched):
        """The link is not a bearer of the workspace's card stream: a tapper
        who is not the minting admin — unlinked, or someone else — is refused
        silently and nothing is written or set."""
        for tapper in (None, "someone-else"):
            patched.tapper_user = tapper
            result = await channel_bind.handle_bind(object(), ctx())
            assert not result.handled and result.reply is None
            assert result.outcome == "tapper_not_minter"
        assert patched.bound == [] and patched.gucs == []

    async def test_a_state_without_a_workspace_is_refused_not_trusted(self, patched):
        patched.state_row = {"user_id": "u1", "workspace_id": None}
        result = await channel_bind.handle_bind(object(), ctx())
        assert not result.handled and result.outcome == "state_without_workspace"


class TestOnceTheTapperIsProvenRefusalsAreAnswered:
    async def test_a_private_chat_is_answered_and_the_state_is_spent(self, patched):
        result = await channel_bind.handle_bind(object(), ctx(chat_type="private"))
        assert result.outcome == "not_a_group" and result.handled
        assert "group picker" in result.reply and "Settings" in result.reply
        assert patched.bound == []

    async def test_a_group_another_workspace_holds_is_answered_by_name(self, patched):
        patched.bind_outcome = bindings.TAKEN
        result = await channel_bind.handle_bind(object(), ctx())
        assert result.outcome == "taken" and result.handled
        assert "another Storydump workspace" in result.reply

    async def test_a_writer_refusal_is_named_and_silent(self, patched, monkeypatch):
        async def bind(session, **kw):
            raise bindings.BindingRefused("external_ref_malformed", "x")

        monkeypatch.setattr(channel_bind.bindings, "bind", bind)
        result = await channel_bind.handle_bind(object(), ctx())
        assert not result.handled and result.outcome == "external_ref_malformed"


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
