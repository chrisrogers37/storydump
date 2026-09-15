"""Session issuance beside the API token (phase 01 of the v2 CLI): a session
value never wears the token prefix, because a bearer that does is routed to
the token resolver and the session would fail every server-side call."""

from __future__ import annotations

from src.services.target import sessions, vocabulary


def test_a_session_value_never_wears_the_token_prefix(monkeypatch):
    draws = iter([vocabulary.TOKEN_PREFIX + "unlucky", "sdt_again", "fine-value"])
    monkeypatch.setattr(sessions.secrets, "token_urlsafe", lambda n: next(draws))
    assert sessions.new_token() == "fine-value"


def test_an_ordinary_draw_is_returned_as_is(monkeypatch):
    monkeypatch.setattr(sessions.secrets, "token_urlsafe", lambda n: "plain")
    assert sessions.new_token() == "plain"


def test_the_real_draw_is_256_bits_url_safe():
    value = sessions.new_token()
    assert len(value) == 43 and not value.startswith(vocabulary.TOKEN_PREFIX)
