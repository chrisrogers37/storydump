"""`callback_tokens` — the ONE module that mints and parses a card button's
`callback_data`.

Mint and parse living apart is how a renamed action ships buttons nothing
consumes; here the round-trip is the test."""

from __future__ import annotations

import uuid

import pytest

from src.services.target import (
    callback_tokens,
    oauth_states,
    prompt_rendering,
    prompts,
    signup_admissions,
    vocabulary,
)

INTENT = str(uuid.uuid4())


class TestRoundTrip:
    @pytest.mark.parametrize("action", callback_tokens.ACTIONS)
    def test_what_the_card_mints_is_what_the_tap_parses(self, action):
        data = prompt_rendering._token(action, INTENT)
        tap = callback_tokens.parse(data)
        assert tap is not None
        assert (tap.version, tap.action, tap.intent_id) == (1, action, INTENT)

    def test_the_card_and_the_parser_share_one_token_function(self):
        assert prompt_rendering._token is callback_tokens.token

    @pytest.mark.parametrize("action", callback_tokens.ACTIONS)
    def test_a_token_stays_within_telegrams_64_bytes(self, action):
        assert len(callback_tokens.token(action, INTENT).encode()) <= 64


class TestRefusals:
    @pytest.mark.parametrize(
        "data",
        [
            None,
            "",
            "v2:post:" + INTENT,  # an unknown version
            "v1:launch:" + INTENT,  # an unknown action
            "v1:post:not-a-uuid",
            "v1:post",  # too few parts
            "v1:post:" + INTENT + ":extra",
            "posted:" + INTENT,  # the legacy shape
        ],
    )
    def test_anything_but_a_known_v1_token_is_none_never_a_raise(self, data):
        assert callback_tokens.parse(data) is None

    def test_the_action_set_is_the_cards(self):
        """Every action a card can mint — the approval card's four, the review
        card's three, and Configure's menu (#1477) — and nothing a card cannot."""
        assert set(callback_tokens.ACTIONS) == set(prompt_rendering._ACTIONS_API) | set(
            prompts.REVIEW_ACTIONS
        ) | set(prompts.MENU_ACTIONS)
        sets = (
            prompt_rendering._ACTIONS_API,
            prompts.REVIEW_ACTIONS,
            prompts.MENU_ACTIONS,
        )
        assert sum(len(x) for x in sets) == len(set().union(*sets)), "disjoint"


class TestTheLinkConfirmationTokens:
    """The identity link's Confirm and Cancel: minted and parsed here too, so
    every button the bot sends has one mint and one parse."""

    STATE = oauth_states.new_state()

    @pytest.mark.parametrize("action", callback_tokens.LINK_ACTIONS)
    def test_round_trip_and_the_64_byte_bound_at_the_longest_user_id(self, action):
        uid = "9" * 20  # longer than any Telegram user id
        data = callback_tokens.link_token(action, self.STATE, uid)
        assert len(data.encode()) <= 64
        assert callback_tokens.parse_link(data) == callback_tokens.LinkTap(
            action=action, state=self.STATE, telegram_user_id=uid
        )

    def test_a_link_token_is_not_a_card_tap_and_a_card_tap_is_not_a_link(self):
        link = callback_tokens.link_token("linkok", self.STATE, "42")
        assert callback_tokens.parse(link) is None
        assert (
            callback_tokens.parse_link(prompt_rendering._token("post", INTENT)) is None
        )

    @pytest.mark.parametrize(
        "data",
        [
            None,
            "",
            "v1:linkok:STATE",  # no user
            "v2:linkok:STATE:42",
            "v1:linkmaybe:STATE:42",
            "v1:linkok:STATE:not-digits",
            "v1:linkok:ST@TE:42",
            "v1:linkok:STATE:42:extra",
        ],
    )
    def test_anything_else_is_none_never_a_raise(self, data):
        assert callback_tokens.parse_link(data) is None

    def test_minting_a_malformed_token_raises(self):
        with pytest.raises(ValueError):
            callback_tokens.link_token("linkok", "a:b", "42")
        with pytest.raises(ValueError):
            callback_tokens.link_token("post", self.STATE, "42")


class TestTheAdmitToken:
    """The waitlist ping's ✅ Admit (#1723): the ref that stands for the
    address, never the address, minted and parsed here like every button."""

    #: The longest address the waitlist holds.
    LONGEST = "a" * 242 + "@example.com"
    REF = signup_admissions.ref("a@example.com")

    def test_round_trip_and_41_bytes_at_the_longest_address(self):
        assert len(self.LONGEST) == vocabulary.MAX_EMAIL_LENGTH
        ref = signup_admissions.ref(self.LONGEST)
        data = callback_tokens.admit_token(ref)
        assert data == f"v1:admit:{ref}" and "@" not in data
        assert len(data.encode()) == 41
        assert callback_tokens.parse_admit(data) == callback_tokens.AdmitTap(ref=ref)

    def test_an_admit_is_no_card_tap_or_link_and_neither_is_an_admit(self):
        admit = callback_tokens.admit_token(self.REF)
        assert callback_tokens.parse(admit) is None
        assert callback_tokens.parse_link(admit) is None
        link = callback_tokens.link_token("linkok", oauth_states.new_state(), "42")
        assert callback_tokens.parse_admit(link) is None
        assert (
            callback_tokens.parse_admit(prompt_rendering._token("post", INTENT)) is None
        )
        assert callback_tokens.ADMIT not in (
            callback_tokens.ACTIONS + callback_tokens.LINK_ACTIONS
        )

    @pytest.mark.parametrize(
        "data",
        [
            None,
            42,
            "",
            "v1:admit",  # no ref
            "v2:admit:" + REF,
            "v1:admitted:" + REF,
            "v1:admit:" + REF.upper(),
            "v1:admit:" + REF[:-1],  # one hex short
            "v1:admit:" + REF + "0",  # one hex long
            "v1:admit:" + "g" * 32,
            "v1:admit:" + REF + ":extra",
        ],
    )
    def test_anything_else_is_none_never_a_raise(self, data):
        assert callback_tokens.parse_admit(data) is None

    @pytest.mark.parametrize(
        "ref",
        [None, "", "0" * 31, "0" * 33, "0" * 64, REF.upper(), "a@example.com"],
    )
    def test_minting_a_malformed_or_overlong_token_raises(self, ref):
        """`"0" * 64`, a whole digest, would make a 73-byte token, past
        Telegram's 64; the ref's shape refuses it before the bound is read."""
        with pytest.raises(ValueError):
            callback_tokens.admit_token(ref)
