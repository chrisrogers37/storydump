"""Telegram identity linking (`07` §2 `link`) — lane A's handler.

Two steps: opening the link only ASKS (a masked email, Confirm and Cancel);
only a Confirm by the same Telegram user, on a live state, links.

`link_identity`'s two refusals are the load-bearing part: they are opposite
facts (`uq_identity_per_provider` vs `uq_user_provider`) that a boolean cannot
tell apart, and silently replacing either would unlink an account by tap.
"""

from __future__ import annotations

import pytest

from src.services.target import callback_tokens, identity, identity_link
from src.services.target.start_router import REFUSAL as StartRouter_REFUSAL
from src.services.target.start_router import StartContext, StartRouter


class _Rowcount:
    """What a real executor returns from an UPDATE: something with a count."""

    def __init__(self, rowcount: int):
        self.rowcount = rowcount


STATE = "STATE1"


def ctx(payload=STATE, uid="4242", name="ada", chat_id=None, chat_type="private"):
    return StartContext(
        payload=payload,
        telegram_user_id=uid,
        # In a private chat the chat id IS the user id.
        chat_id=uid if chat_id is None else chat_id,
        chat_type=chat_type,
        display_name=name,
    )


def link_tap(action=callback_tokens.LINK_CONFIRM, state=STATE, offered_to="4242"):
    return callback_tokens.LinkTap(
        action=action, state=state, telegram_user_id=offered_to
    )


class _Fake:
    """A scripted `oauth_states` (one live row, CAS-consumed exactly as the
    real one is) and a recording `link_identity`."""

    def __init__(self):
        self.live = {STATE: {"user_id": "user-1"}}
        self.consumed: list[tuple[str, dict]] = []
        self.peeked: list[dict] = []
        self.link = True
        self.linked: list[dict] = []
        self.account = {
            "primary_email": "ada.lovelace@example.com",
            "display_name": None,
        }


@pytest.fixture()
def patched(monkeypatch):
    f = _Fake()

    async def peek(conn, **kw):
        f.peeked.append(kw)
        if kw["state"] not in f.live:
            raise identity_link.oauth_states.OAuthStateRefused("unknown state")
        return f.live[kw["state"]]

    async def consume(conn, **kw):
        row = f.live.pop(kw["state"], None)  # one-shot, like the CAS
        if row is None:
            raise identity_link.oauth_states.OAuthStateRefused("consumed or unknown")
        f.consumed.append((kw["state"], kw))
        return row

    async def link(conn, **kw):
        f.linked.append(kw)
        if isinstance(f.link, Exception):
            raise f.link
        return f.link

    async def row(conn, sql, **params):
        return f.account

    monkeypatch.setattr(identity_link.oauth_states, "peek_live_state", peek)
    monkeypatch.setattr(identity_link.oauth_states, "consume_state", consume)
    monkeypatch.setattr(identity_link.identity, "link_identity", link)
    monkeypatch.setattr(identity_link.readers, "row", row)
    return f


async def tapped(action=callback_tokens.LINK_CONFIRM, *, by="4242", **kw):
    """A tap on the prompt, in the private chat of *by*."""
    return await identity_link.handle_tap(
        None,
        link_tap(action, **kw),
        from_user_id=by,
        chat_ref=by,
        chat_type="private",
        display_name="ada",
    )


class TestOpeningTheLinkOnlyAsks:
    """The fix for link-by-tap: opening the link attached the opener's
    Telegram to whoever minted it. Now it names the account and links
    nothing."""

    @pytest.mark.asyncio
    async def test_opening_the_link_shows_a_confirmation_and_links_nothing(
        self, patched
    ):
        r = await identity_link.handle_link(None, ctx())
        assert (r.outcome, r.handled) == ("confirmation_offered", True)
        assert "a•••@example.com" in r.reply
        assert patched.linked == [], "opening the link linked an identity"
        assert patched.consumed == [], "opening the link spent the state"
        assert STATE in patched.live

    @pytest.mark.asyncio
    async def test_the_prompt_carries_confirm_and_cancel_for_this_state_and_user(
        self, patched
    ):
        r = await identity_link.handle_link(None, ctx())
        (row,) = r.reply_markup["inline_keyboard"]
        taps = [callback_tokens.parse_link(b["callback_data"]) for b in row]
        assert [t.action for t in taps] == ["linkok", "linkno"]
        assert {(t.state, t.telegram_user_id) for t in taps} == {(STATE, "4242")}
        assert [b["text"] for b in row] == [
            identity_link.CONFIRM_LABEL,
            identity_link.CANCEL_LABEL,
        ]

    @pytest.mark.asyncio
    async def test_the_full_address_is_never_shown(self, patched):
        r = await identity_link.handle_link(None, ctx())
        assert "ada.lovelace@example.com" not in r.reply
        assert "ada.lovelace" not in r.reply

    @pytest.mark.asyncio
    async def test_an_account_without_email_is_named_by_its_display_name(self, patched):
        patched.account = {"primary_email": None, "display_name": "Ada L"}
        r = await identity_link.handle_link(None, ctx())
        assert "Ada L" in r.reply

    @pytest.mark.asyncio
    async def test_the_state_is_peeked_pinned_to_purpose_AND_provider(self, patched):
        """Disjointness is enforced at the lookup, not by the prefix alone —
        an `inv-` token reaching here must not read as a link state."""
        await identity_link.handle_link(None, ctx())
        assert patched.peeked[0]["expected_purpose"] == "link"
        assert patched.peeked[0]["expected_provider"] == "telegram"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "context",
        [
            ctx(chat_id="-100777", chat_type="supergroup"),
            ctx(chat_id="-5", chat_type="group"),
            ctx(chat_id="999"),  # "private" but not the sender's own chat
        ],
        ids=["supergroup", "group", "private-not-own"],
    )
    async def test_only_the_openers_own_private_chat_is_served(self, patched, context):
        r = await identity_link.handle_link(None, context)
        assert (r.outcome, r.handled, r.reply) == ("not_private", False, None)
        assert patched.peeked == [] and patched.linked == []


class TestConfirmingLinks:
    @pytest.mark.asyncio
    async def test_confirm_links_the_tappers_identity_to_the_pinned_user(self, patched):
        r = await tapped()
        assert r.outcome == "linked"
        (linked,) = patched.linked
        assert linked["user_id"] == "user-1" and linked["external_id"] == "4242"
        assert linked["provider"] == "telegram"
        assert "a•••@example.com" in r.edit_text
        assert "ada.lovelace@example.com" not in r.edit_text

    @pytest.mark.asyncio
    async def test_the_state_is_consumed_pinned_to_purpose_AND_provider(self, patched):
        await tapped()
        ((state, kw),) = patched.consumed
        assert state == STATE
        assert kw["expected_purpose"] == "link"
        assert kw["expected_provider"] == "telegram"

    @pytest.mark.asyncio
    async def test_the_state_cannot_be_confirmed_twice(self, patched):
        first = await tapped()
        second = await tapped()
        assert first.outcome == "linked"
        assert second.outcome == "state_refused"
        assert second.answer_text == identity_link.REFUSAL
        assert len(patched.linked) == 1

    @pytest.mark.asyncio
    async def test_an_already_existing_link_to_the_same_user_is_not_an_error(
        self, patched
    ):
        patched.link = False  # the row already existed
        assert (await tapped()).outcome == "already_linked"


class TestCancellingAndRefusing:
    @pytest.mark.asyncio
    async def test_cancel_links_nothing_and_spends_the_state(self, patched):
        r = await tapped(callback_tokens.LINK_CANCEL)
        assert r.outcome == "cancelled" and r.edit_text == identity_link.CANCELLED
        assert patched.linked == []
        assert (await tapped()).outcome == "state_refused", "confirm after cancel"
        assert patched.linked == []

    @pytest.mark.asyncio
    async def test_a_confirm_from_another_telegram_user_is_refused(self, patched):
        """A forwarded prompt tapped by someone else: the button names the
        user it was offered to, and that is not them."""
        r = await tapped(by="5555")
        assert r.outcome == "tapper_mismatch"
        assert r.answer_text == identity_link.REFUSAL and r.edit_text is None
        assert patched.linked == [] and patched.consumed == []
        assert STATE in patched.live, "a refused tap must not spend the state"

    @pytest.mark.asyncio
    async def test_a_confirm_outside_the_users_private_chat_is_refused(self, patched):
        r = await identity_link.handle_tap(
            None,
            link_tap(),
            from_user_id="4242",
            chat_ref="-100777",
            chat_type="supergroup",
        )
        assert r.outcome == "tapper_mismatch"
        assert patched.linked == [] and patched.consumed == []

    @pytest.mark.asyncio
    async def test_an_expired_or_unknown_state_reads_as_the_routers_refusal(
        self, patched
    ):
        r = await tapped(state="NOPE")
        assert r.outcome == "state_refused"
        assert r.answer_text == identity_link.REFUSAL == StartRouter_REFUSAL
        assert patched.linked == []

    @pytest.mark.asyncio
    async def test_a_refused_open_yields_no_reply_text(self, patched):
        r = await identity_link.handle_link(None, ctx(payload="NOPE"))
        assert r.handled is False
        assert r.reply is None and r.reply_markup is None
        assert r.outcome == "state_refused"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "reason", ["identity_held_by_another_user", "user_already_has_this_provider"]
    )
    async def test_either_half_already_linked_is_refused_by_name(self, patched, reason):
        """Opposite facts (`uq_identity_per_provider` vs `uq_user_provider`):
        named apart in the outcome, one string to the tapper. Replacing
        silently would unlink an account by tap."""
        patched.link = identity.IdentityAlreadyLinked(reason)
        r = await tapped()
        assert r.outcome == reason
        assert r.answer_text == identity_link.ALREADY_LINKED_ELSEWHERE

    @pytest.mark.asyncio
    async def test_a_link_state_with_a_null_user_is_refused_not_trusted(self, patched):
        """`ck_oauth_state_context` makes this unreachable. If it is ever
        reached, the CHECK is gone and trusting the row would attach an
        identity to nobody."""
        patched.live[STATE] = {"user_id": None}
        r = await identity_link.handle_link(None, ctx())
        assert r.outcome == "state_without_user" and r.handled is False
        assert (await tapped()).outcome == "state_without_user"
        assert patched.linked == [], "wrote an identity for a NULL user"


class TestTheMask:
    @pytest.mark.parametrize(
        "email,masked",
        [
            ("ada@gmail.com", "a•••@gmail.com"),
            ("ada.lovelace+x@example.co.uk", "a•••@example.co.uk"),
            ("a@b.io", "a•••@b.io"),
            ("odd@name@example.com", "o•••@example.com"),
        ],
    )
    def test_first_character_then_the_domain(self, email, masked):
        assert identity_link.mask_email(email) == masked
        assert email not in identity_link.confirmation_text(masked)

    @pytest.mark.parametrize("email", [None, "", "no-at-sign", "@example.com", "x@"])
    def test_not_an_address_is_not_masked(self, email):
        assert identity_link.mask_email(email) is None


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
                return _Rowcount(1)

        async def issue_state(conn, **kw):
            statements.append(("ISSUE", kw))
            return "st4te"

        monkeypatch.setattr(identity_link.oauth_states, "issue_state", issue_state)
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
