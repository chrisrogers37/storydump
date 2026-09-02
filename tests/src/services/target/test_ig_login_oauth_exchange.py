"""#1041 — the Instagram Login code exchange, beside its Drive sibling.

**Bound: nothing here contacts Instagram.** The transport is stubbed at the
egress floor, so these prove the two-leg logic and its refusals, not that Meta
answers as modelled.
"""

from __future__ import annotations

import pytest

from src.services.target import ig_login_oauth


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class TestTheExchangeLeg:
    """The two-leg swap. A short-lived token is refused rather than stored:
    it dies within the hour and the refresh leg cannot rescue it."""

    @staticmethod
    def _client(monkeypatch, responses):
        seen = []

        async def request(client, method, url, **kw):
            seen.append((method, url, kw.get("data") or kw.get("params")))
            return responses.pop(0)

        from src.services.target import egress

        monkeypatch.setattr(egress, "request", request)
        return seen

    async def test_it_swaps_the_short_lived_token_for_a_long_lived_one(
        self, monkeypatch
    ):
        seen = self._client(
            monkeypatch,
            [
                _Resp(200, {"access_token": "short", "user_id": 987}),
                _Resp(200, {"access_token": "long", "expires_in": 5184000}),
            ],
        )
        grant = await ig_login_oauth.exchange_code(
            None, code="c", redirect_uri="r", client_id="i", client_secret="s"
        )
        assert grant.access_token == "long", "the short-lived token was stored"
        assert grant.ig_user_id == "987"
        assert grant.expires_at is not None
        assert seen[0][1] == ig_login_oauth.TOKEN_URL
        assert seen[1][1] == ig_login_oauth.LONG_LIVED_URL

    async def test_the_user_id_is_carried_because_only_the_first_leg_has_it(
        self, monkeypatch
    ):
        """It is the real Instagram identity, and the only thing that
        distinguishes a connected destination from a typed `manual:<handle>`."""
        self._client(
            monkeypatch,
            [
                _Resp(200, {"access_token": "short", "user_id": "17841400000000000"}),
                _Resp(200, {"access_token": "long"}),
            ],
        )
        grant = await ig_login_oauth.exchange_code(
            None, code="c", redirect_uri="r", client_id="i", client_secret="s"
        )
        assert grant.ig_user_id == "17841400000000000"
        assert grant.expires_at is None, "no expires_in must not invent an expiry"

    @pytest.mark.parametrize(
        "responses,reason",
        [
            ([_Resp(400, {"error": "x"})], "exchange_failed"),
            ([_Resp(200, None)], "malformed_response"),
            ([_Resp(200, {"user_id": 1})], "malformed_response"),
            ([_Resp(200, {"access_token": "s"})], "malformed_response"),
            (
                [_Resp(200, {"access_token": "s", "user_id": 1}), _Resp(400, {})],
                "no_long_lived_token",
            ),
            (
                [_Resp(200, {"access_token": "s", "user_id": 1}), _Resp(200, {})],
                "no_long_lived_token",
            ),
        ],
        ids=[
            "token endpoint 400",
            "body is not json",
            "no access_token",
            "no user_id",
            "long-lived swap 400",
            "long-lived swap returns no token",
        ],
    )
    async def test_it_refuses_by_name_rather_than_storing_something_unusable(
        self, monkeypatch, responses, reason
    ):
        self._client(monkeypatch, responses)
        with pytest.raises(ig_login_oauth.IgLoginRefused) as exc:
            await ig_login_oauth.exchange_code(
                None, code="c", redirect_uri="r", client_id="i", client_secret="s"
            )
        assert exc.value.reason == reason

    async def test_a_non_200_swap_is_refused_even_when_it_carries_a_token(
        self, monkeypatch
    ):
        """Found by mutation: deleting the status check on the long-lived swap
        left every existing test green, because an empty error body trips the
        downstream missing-token guard anyway. It does NOT trip on an error
        response that happens to carry an `access_token` — which would then be
        stored as though the swap had succeeded. The status is checked on its
        own merits, and this is the case that says so."""
        self._client(
            monkeypatch,
            [
                _Resp(200, {"access_token": "short", "user_id": 1}),
                _Resp(400, {"access_token": "not-a-real-grant", "error": "x"}),
            ],
        )
        with pytest.raises(ig_login_oauth.IgLoginRefused) as exc:
            await ig_login_oauth.exchange_code(
                None, code="c", redirect_uri="r", client_id="i", client_secret="s"
            )
        assert exc.value.reason == "no_long_lived_token"

    async def test_no_token_ever_appears_in_a_refusal_message(self, monkeypatch):
        """The message reaches a log, on a path whose whole job is tokens."""
        self._client(
            monkeypatch,
            [
                _Resp(200, {"access_token": "SECRET-TOKEN", "user_id": 1}),
                _Resp(400, {}),
            ],
        )
        with pytest.raises(ig_login_oauth.IgLoginRefused) as exc:
            await ig_login_oauth.exchange_code(
                None, code="c", redirect_uri="r", client_id="i", client_secret="s"
            )
        assert "SECRET-TOKEN" not in str(exc.value)
