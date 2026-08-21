"""W5d door classification — the three-way routing IS the safety property.

The gate (`tests/scripts/test_w5de_credential_lifecycle.py`) proves the
executors against the real schema; these units prove the door's CLASSIFIER,
because the classifier decides whether a credential lives, dies, or retries —
and a misroute in either direction is the defect (dead-on-a-blip strands an
account; retry-on-rejection hides a dead credential for five attempts).
"""

from datetime import datetime, timezone

import pytest

from src.services.target import credential_lifecycle as cl


class _Resp:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body


def _door_with(monkeypatch, resp):
    async def fake_request(client, method, url, *, policy=None, **kwargs):
        return resp

    monkeypatch.setattr(cl.egress, "request", fake_request)


class TestClassification:
    @pytest.mark.asyncio
    async def test_200_with_token_returns_it_and_computes_expiry(self, monkeypatch):
        _door_with(
            monkeypatch, _Resp(200, {"access_token": "tok-N", "expires_in": 3600})
        )
        token, expires = await cl.ig_refresh("tok-O")
        assert token == "tok-N"
        assert expires is not None and expires > datetime.now(timezone.utc)

    @pytest.mark.asyncio
    async def test_200_without_token_is_transient_never_an_answer(self, monkeypatch):
        _door_with(monkeypatch, _Resp(200, {}))
        with pytest.raises(cl.RefreshTransient):
            await cl.ig_refresh("tok-O")

    @pytest.mark.parametrize("status", [400, 401, 403])
    @pytest.mark.asyncio
    async def test_auth_class_statuses_are_definitive(self, monkeypatch, status):
        _door_with(monkeypatch, _Resp(status))
        with pytest.raises(cl.RefreshRejected) as exc_info:
            await cl.ig_refresh("tok-SECRET")
        assert exc_info.value.status_code == status

    @pytest.mark.parametrize("status", [429, 500, 502, 503])
    @pytest.mark.asyncio
    async def test_faults_are_transient_by_d31(self, monkeypatch, status):
        _door_with(monkeypatch, _Resp(status))
        with pytest.raises(cl.RefreshTransient):
            await cl.ig_refresh("tok-SECRET")

    @pytest.mark.asyncio
    async def test_the_token_never_appears_in_either_raise(self, monkeypatch):
        """Redaction is substring-checked on str AND repr, both classes."""
        for status, exc_type in ((401, cl.RefreshRejected), (503, cl.RefreshTransient)):
            _door_with(monkeypatch, _Resp(status))
            with pytest.raises(exc_type) as exc_info:
                await cl.ig_refresh("tok-SECRET-VALUE")
            for surface in (str(exc_info.value), repr(exc_info.value)):
                assert "tok-SECRET-VALUE" not in surface
