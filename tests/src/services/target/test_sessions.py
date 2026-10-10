"""Session issuance beside the API token (phase 01 of the v2 CLI): a session
value never wears the token prefix, because a bearer that does is routed to
the token resolver and the session would fail every server-side call."""

from __future__ import annotations

import pytest

from src.config.settings import Settings, settings
from src.exceptions.tenancy import TenantResolutionError
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


class _Recorder:
    """Records each statement and its parameters; answers one row (or none)
    and a list of rows — the shapes `resolve` and `revoke_all_for_user` read —
    and a rowcount for `oauth_states.retire_live_states`."""

    def __init__(self, row=None, rows=()):
        self.calls = []
        self.row, self.rows, self.rowcount = row, list(rows), 0

    async def execute(self, stmt, params):
        self.calls.append((str(stmt), params))
        return self

    def first(self):
        return self.row

    def all(self):
        return self.rows


LIVE_ROW = ("sess-1", "user-1", False, False, "active")


class TestTheAbsoluteLifetime:
    """`created_at` + `SESSION_MAX_AGE_SECONDS` bounds a session however often
    it is used. The DB gate (`test_identity_writers.py`) proves the statement
    against PostgreSQL; this pins what it is handed."""

    async def test_resolve_binds_the_setting_by_default(self, monkeypatch):
        monkeypatch.setattr(settings, "SESSION_MAX_AGE_SECONDS", 1234)
        ex = _Recorder(row=LIVE_ROW)
        await sessions.resolve(ex, token_hash="h")
        sql, params = ex.calls[0]
        assert params == {
            "h": "h",
            "ttl": sessions.SESSION_TTL_SECONDS,
            "max_age": 1234,
            "throttle": sessions.RENEW_THROTTLE_SECONDS,
        }
        # Expired on either clock, and the slide is clamped to the cap.
        assert "created_at + make_interval(secs => :max_age) <= now()" in sql
        assert "LEAST(now() + make_interval(secs => :ttl)" in sql
        assert "s.created_at + make_interval(secs => :max_age))" in sql

    async def test_an_explicit_age_wins(self):
        ex = _Recorder(row=LIVE_ROW)
        await sessions.resolve(ex, token_hash="h", max_age_seconds=60)
        assert ex.calls[0][1]["max_age"] == 60

    def test_the_default_matches_the_cookie(self):
        """30 days: the cookie's Max-Age and the Privacy page's "30 days"."""
        default = Settings.model_fields["SESSION_MAX_AGE_SECONDS"].default
        assert default == sessions.SESSION_TTL_SECONDS == 30 * 24 * 3600


class TestADisabledPersonIsRefusedAtTheGate:
    """The web's half of "a disabled person is refused on every channel"
    (#1572): a live, unexpired session of a person who is not `active` is
    refused by name, and its slide is gated on the same state. The DB gate
    (`test_identity_writers.py`) proves it against PostgreSQL."""

    async def test_a_live_session_of_a_disabled_person_is_refused(self):
        ex = _Recorder(row=("sess-1", "user-1", False, False, "disabled"))

        with pytest.raises(TenantResolutionError) as refused:
            await sessions.resolve(ex, token_hash="h")

        assert refused.value.reason == "disabled_user"
        sql, _ = ex.calls[0]
        assert "AND s.state = 'active'" in sql, "the slide is not gated on the state"

    async def test_the_same_session_resolves_while_the_person_is_active(self):
        session = await sessions.resolve(_Recorder(row=LIVE_ROW), token_hash="h")
        assert (session.id, session.user_id) == ("sess-1", "user-1")


class TestRevokeAllForUser:
    async def test_it_keys_on_the_presented_live_session_and_counts(self):
        ex = _Recorder(rows=[("user-1",)] * 3)
        assert await sessions.revoke_all_for_user(ex, token_hash="h") == 3
        (sql, params), (link_sql, link_params) = ex.calls
        # The person's pending Telegram link states are retired with them.
        assert "UPDATE oauth_states SET consumed_at = now()" in link_sql
        assert link_params == {
            "provider": "telegram",
            "purpose": "link",
            "uid": "user-1",
        }
        assert params == {"h": "h", "max_age": settings.SESSION_MAX_AGE_SECONDS}
        # The user is named only through a LIVE presented session, and rows
        # already revoked keep their first instant.
        for clause in (
            "p.token_hash = :h AND p.revoked_at IS NULL",
            "p.expires_at > now()",
            "p.created_at + make_interval(secs => :max_age) > now()",
            "t.user_id = p.user_id AND t.revoked_at IS NULL",
        ):
            assert clause in sql, clause

    async def test_a_dead_presented_session_revokes_and_retires_nothing(self):
        ex = _Recorder(rows=[])
        assert await sessions.revoke_all_for_user(ex, token_hash="h") == 0
        assert len(ex.calls) == 1, "no link states are touched for nobody"
