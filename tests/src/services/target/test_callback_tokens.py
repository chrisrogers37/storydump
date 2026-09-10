"""`callback_tokens` — the ONE module that mints and parses a card button's
`callback_data` (phase 1 of the 2026-09-09 tap plan, step 3).

Mint and parse living apart is how a renamed action ships buttons nothing
consumes; here the round-trip is the test."""

from __future__ import annotations

import uuid

import pytest

from src.services.target import callback_tokens, prompts

INTENT = str(uuid.uuid4())


class TestRoundTrip:
    @pytest.mark.parametrize("action", callback_tokens.ACTIONS)
    def test_what_the_card_mints_is_what_the_tap_parses(self, action):
        data = prompts._token(action, INTENT)
        tap = callback_tokens.parse(data)
        assert tap is not None
        assert (tap.version, tap.action, tap.intent_id) == (1, action, INTENT)

    def test_the_card_and_the_parser_share_one_token_function(self):
        assert prompts._token is callback_tokens.token

    def test_a_token_stays_within_telegrams_64_bytes(self):
        assert len(callback_tokens.token("posted", INTENT).encode()) <= 64


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
        assert set(callback_tokens.ACTIONS) == set(prompts._ACTIONS_API)
