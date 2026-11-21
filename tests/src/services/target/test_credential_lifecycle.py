"""Door classification — the three-way routing IS the safety property.

The gate (`tests/scripts/test_w5de_credential_lifecycle.py`) proves the
executors against the real schema; these units prove the door's CLASSIFIER,
because the classifier decides whether a credential lives, dies, or retries —
and a misroute in either direction is the defect (dead-on-a-blip strands an
account; retry-on-rejection hides a dead credential for five attempts).
"""

from datetime import datetime, timezone

import httpx
import pytest

from src.services.target import credential_lifecycle as cl
from src.services.target import meta_adapter


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
    async def test_auth_class_statuses_without_a_code_are_definitive(
        self, monkeypatch, status
    ):
        _door_with(monkeypatch, _Resp(status))
        with pytest.raises(cl.RefreshRejected) as exc_info:
            await cl.ig_refresh("tok-SECRET")
        assert exc_info.value.status_code == status

    @pytest.mark.parametrize(
        ("code", "raised"), [(4, cl.RefreshTransient), (190, cl.RefreshRejected)]
    )
    @pytest.mark.asyncio
    async def test_a_temporary_code_is_transient_and_a_final_one_definitive(
        self, monkeypatch, code, raised
    ):
        """On one status, Meta's error code decides the route: 4 (API Too Many
        Calls) is transient, for the ladder to retry, and 190 (a bad token) is
        definitive."""
        _door_with(monkeypatch, _Resp(400, {"error": {"code": code}}))
        with pytest.raises(raised) as exc_info:
            await cl.ig_refresh("tok-SECRET")
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_a_rejection_carries_metas_four_fields_and_nothing_else(
        self, monkeypatch, caplog
    ):
        """What the "definitively rejected" line prints: Meta's code, subcode,
        type and trace id, the token the request carried redacted from the two
        text fields and no control character left in them, and never the
        token or Meta's text."""
        caplog.set_level("DEBUG")
        error = {
            "message": "message-text-SENTINEL tok-SENTINEL",
            "type": "OAuth\r\nException tok-SENTINEL",
            "code": 190,
            "error_subcode": 460,
            "fbtrace_id": "AbC123xyz tok-SENTINEL",
        }
        _door_with(monkeypatch, _Resp(400, {"error": error}))
        with pytest.raises(cl.RefreshRejected) as exc_info:
            await cl.ig_refresh("tok-SENTINEL")
        rejected = exc_info.value
        assert rejected.meta_error == (
            "Meta error code=190 subcode=460 type=OAuthException <token>"
            " fbtrace_id=AbC123xyz <token>"
        )
        said = [str(rejected), repr(rejected), rejected.meta_error]
        said += [record.getMessage() for record in caplog.records]
        for sentinel in ("tok-SENTINEL", "message-text-SENTINEL"):
            assert not [line for line in said if sentinel in line]

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param(b"<html>denied</html>", id="not-json"),
            pytest.param(b"[" * 100_000 + b"]" * 100_000, id="nested-past-the-parser"),
            pytest.param(b"[]", id="not-an-object"),
            pytest.param(b'{"error": {"message": "refused"}}', id="no-field-it-reads"),
            pytest.param(b'{"error": {"code": Infinity}}', id="an-infinite-code"),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_refusal_with_no_readable_code_is_definitive(
        self, monkeypatch, body
    ):
        """A refusal whose body is not JSON, not an object, or whose error
        object holds nothing the reader reads is a rejection, its fields
        unread: neither the code's read nor the fields' raises."""
        _door_with(monkeypatch, httpx.Response(400, content=body))
        with pytest.raises(cl.RefreshRejected) as exc_info:
            await cl.ig_refresh("tok-SECRET")
        assert exc_info.value.status_code == 400
        assert exc_info.value.meta_error == meta_adapter.ERROR_FIELDS_UNREAD

    @pytest.mark.parametrize("status", [429, 500, 502, 503])
    @pytest.mark.asyncio
    async def test_faults_are_transient(self, monkeypatch, status):
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
