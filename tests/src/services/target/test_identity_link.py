"""Telegram identity linking (`07` §2 `link`) — lane A's handler.

`link_identity`'s two refusals are the load-bearing part: they are opposite
facts (`uq_identity_per_provider` vs `uq_user_provider`) that a boolean cannot
tell apart, and silently replacing either would unlink an account by tap.
"""

from __future__ import annotations

import pytest

from src.services.target import identity, identity_link
from src.services.target.start_router import StartContext, StartRouter


def ctx(payload="STATE1", uid="tg-42", name="ada"):
    return StartContext(
        payload=payload,
        telegram_user_id=uid,
        chat_id="99",
        chat_type="private",
        display_name=name,
    )


class _Fake:
    """Records calls; scripts consume_state and link_identity."""

    def __init__(self, *, state_row=None, refuse=None, link=None):
        self.state_row, self.refuse, self.link = state_row, refuse, link
        self.linked = []


@pytest.fixture()
def patched(monkeypatch):
    f = _Fake()

    async def consume(conn, **kw):
        f.consume_kw = kw
        if f.refuse:
            raise identity_link.ig_login_oauth.OAuthStateRefused(f.refuse)
        return f.state_row

    async def link(conn, **kw):
        f.linked.append(kw)
        if isinstance(f.link, Exception):
            raise f.link
        return f.link

    monkeypatch.setattr(identity_link.ig_login_oauth, "consume_state", consume)
    monkeypatch.setattr(identity_link.identity, "link_identity", link)
    return f


class TestTheHappyPath:
    @pytest.mark.asyncio
    async def test_a_valid_state_links_the_tapping_identity_to_the_pinned_user(
        self, patched
    ):
        patched.state_row = {"user_id": "user-1"}
        patched.link = True
        r = await identity_link.handle_link(None, ctx())
        assert (r.outcome, r.handled) == ("linked", True)
        assert patched.linked[0]["user_id"] == "user-1"
        assert patched.linked[0]["external_id"] == "tg-42"
        assert patched.linked[0]["provider"] == "telegram"

    @pytest.mark.asyncio
    async def test_the_state_is_consumed_pinned_to_purpose_AND_provider(self, patched):
        """Disjointness is enforced at the lookup, not by the prefix alone —
        an `inv-` token reaching here must not consume as a link state."""
        patched.state_row = {"user_id": "u"}
        patched.link = True
        await identity_link.handle_link(None, ctx())
        assert patched.consume_kw["expected_purpose"] == "link"
        assert patched.consume_kw["expected_provider"] == "telegram"

    @pytest.mark.asyncio
    async def test_a_second_tap_of_the_same_link_is_idempotent_not_an_error(
        self, patched
    ):
        patched.state_row = {"user_id": "u"}
        patched.link = False  # row already existed
        r = await identity_link.handle_link(None, ctx())
        assert (r.outcome, r.handled) == ("already_linked", True)


class TestEveryRefusalIsNamedAndSilent:
    @pytest.mark.asyncio
    async def test_a_refused_state_yields_no_reply_text(self, patched):
        patched.refuse = "consumed"
        r = await identity_link.handle_link(None, ctx())
        assert r.handled is False
        assert r.reply is None, "a refusal leaked copy past the router"
        assert r.outcome == "state_refused"

    @pytest.mark.asyncio
    async def test_an_identity_held_by_another_user_is_refused_by_name(self, patched):
        patched.state_row = {"user_id": "u"}
        patched.link = identity.IdentityAlreadyLinked("identity_held_by_another_user")
        r = await identity_link.handle_link(None, ctx())
        assert r.outcome == "identity_held_by_another_user"
        assert r.handled is False and r.reply is None

    @pytest.mark.asyncio
    async def test_a_user_who_already_linked_a_different_account_is_refused(
        self, patched
    ):
        """The OPPOSITE fact from the test above, and a boolean could not tell
        them apart. Replacing silently would unlink an account by tap."""
        patched.state_row = {"user_id": "u"}
        patched.link = identity.IdentityAlreadyLinked("user_already_has_this_provider")
        r = await identity_link.handle_link(None, ctx())
        assert r.outcome == "user_already_has_this_provider"

    @pytest.mark.asyncio
    async def test_a_link_state_with_a_null_user_is_refused_not_trusted(self, patched):
        """`ck_oauth_state_context` makes this unreachable. If it is ever
        reached, the CHECK is gone and trusting the row would attach an
        identity to nobody."""
        patched.state_row = {"user_id": None}
        r = await identity_link.handle_link(None, ctx())
        assert r.outcome == "state_without_user" and r.handled is False
        assert patched.linked == [], "wrote an identity for a NULL user"


class TestTheDeepLinkAndRegistration:
    def test_the_deep_link_carries_the_disjoint_prefix(self):
        url = identity_link.deep_link("sd_bot", "ABC")
        assert url == "https://t.me/sd_bot?start=link-ABC"

    def test_registering_puts_it_behind_the_link_prefix(self):
        r = StartRouter()
        identity_link.register(r)
        assert "link-" in r._handlers

    def test_lane_c_can_register_alongside_it(self):
        """The whole point of one generic door."""

        async def inv(conn, c): ...

        r = StartRouter()
        identity_link.register(r)
        r.register("inv-", inv)  # must not raise
        assert set(r._handlers) == {"link-", "inv-"}


class TestIssuingRetiresTheUsersEarlierLinks:
    """One live link per user: a copy pasted into a chat stops working the
    moment a new one is minted (the `07` §2 last-issued-wins rule)."""

    async def test_the_users_other_live_link_states_are_consumed_before_issuing(
        self, monkeypatch
    ):
        statements = []

        class _Conn:
            async def execute(self, statement, params=None):
                statements.append((str(statement), params))

        async def issue_state(conn, **kw):
            statements.append(("ISSUE", kw))
            return "st4te"

        monkeypatch.setattr(identity_link.ig_login_oauth, "issue_state", issue_state)
        link = await identity_link.issue_link_state(
            _Conn(), user_id="user-1", bot_username="storydump_app_bot"
        )
        assert link == "https://t.me/storydump_app_bot?start=link-st4te"
        (retire, _), (issue, kw) = statements
        assert "UPDATE oauth_states SET consumed_at = now()" in retire
        assert "consumed_at IS NULL" in retire and "user_id = :uid" in retire
        assert (
            issue == "ISSUE" and kw["user_id"] == "user-1" and kw["purpose"] == "link"
        )
