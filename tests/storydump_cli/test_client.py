"""The HTTP client against a scripted API (``httpx.MockTransport``).

The server side is being built beside this; the contract these tests pin is
the wire shape both sides agreed: bearer auth, the ``/api/v1`` prefix, the
error bodies and how each becomes an ``ApiError`` (401 without a reason,
403 with one, 404 without), and that a connect error or a 5xx is
``Unreachable`` — never a traceback.
"""

from __future__ import annotations

import httpx
import pytest

from storydump_cli import __version__
from storydump_cli.client import ApiError, Client, Unreachable, InsecureApiUrl

SECRET = "sdt_" + "a" * 43
WS = "11111111-1111-4111-8111-111111111111"
TOKEN_ID = "33333333-3333-4333-8333-333333333333"


def _client(handler, token=SECRET) -> Client:
    return Client("https://api.test", token, transport=httpx.MockTransport(handler))


def _recording(status=200, body=None):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status, json=body if body is not None else {})

    return handler, seen


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000",
        "http://localhost:3000",
        "http://[::1]:8000",
        "https://api.test",
    ],
)
def test_loopback_http_and_any_https_are_allowed(url):
    Client(url, SECRET, transport=httpx.MockTransport(lambda r: httpx.Response(200)))


@pytest.mark.parametrize(
    "url", ["http://api.storydump.app", "api.storydump.app", "ftp://x"]
)
def test_a_bearer_never_goes_over_plain_http_off_this_machine(url):
    with pytest.raises(InsecureApiUrl) as exc:
        Client(
            url, SECRET, transport=httpx.MockTransport(lambda r: httpx.Response(200))
        )
    assert "plain http" in str(exc.value) and "STORYDUMP_INSECURE_HTTP" in str(
        exc.value
    )


def test_the_insecure_http_override_is_explicit():
    Client(
        "http://api.storydump.app",
        SECRET,
        transport=httpx.MockTransport(lambda r: httpx.Response(200)),
        allow_insecure_http=True,
    )


def test_sends_the_bearer_the_user_agent_and_accept():
    handler, seen = _recording(200, {"kind": "token"})
    assert _client(handler).principal() == {"kind": "token"}
    (request,) = seen
    assert request.method == "GET"
    assert str(request.url) == "https://api.test/api/v1/me/principal"
    assert request.headers["authorization"] == f"Bearer {SECRET}"
    assert request.headers["user-agent"] == f"storydump-cli/{__version__}"
    assert request.headers["accept"] == "application/json"


def test_a_trailing_slash_on_the_base_url_is_harmless():
    handler, seen = _recording(200, {"kind": "token"})
    Client(
        "https://api.test/", SECRET, transport=httpx.MockTransport(handler)
    ).principal()
    assert str(seen[0].url) == "https://api.test/api/v1/me/principal"


def test_without_a_token_no_authorization_header_is_sent():
    handler, seen = _recording(200, {"kind": "session"})
    _client(handler, token=None).principal()
    assert "authorization" not in seen[0].headers


def test_the_token_routes():
    handler, seen = _recording(200, {"tokens": []})
    client = _client(handler)
    client.list_my_tokens()
    client.revoke_my_token(TOKEN_ID)
    client.list_workspace_tokens(WS)
    client.revoke_workspace_token(WS, TOKEN_ID)
    assert [(r.method, r.url.path) for r in seen] == [
        ("GET", "/api/v1/me/tokens"),
        ("DELETE", f"/api/v1/me/tokens/{TOKEN_ID}"),
        ("GET", f"/api/v1/workspaces/{WS}/tokens"),
        ("DELETE", f"/api/v1/workspaces/{WS}/tokens/{TOKEN_ID}"),
    ]


def test_401_is_an_api_error_without_a_reason():
    handler, _ = _recording(401, {"detail": "authentication required"})
    with pytest.raises(ApiError) as caught:
        _client(handler).principal()
    assert not isinstance(caught.value, Unreachable)
    assert (caught.value.status, caught.value.reason) == (401, None)
    assert caught.value.detail == "authentication required"


def test_403_carries_the_reason():
    handler, _ = _recording(
        403, {"detail": "this token is read-only", "reason": "readonly_token"}
    )
    with pytest.raises(ApiError) as caught:
        _client(handler).list_my_tokens()
    assert (caught.value.status, caught.value.reason) == (403, "readonly_token")


def test_404_is_status_404():
    handler, _ = _recording(404, {"detail": "not found"})
    with pytest.raises(ApiError) as caught:
        _client(handler).revoke_my_token(TOKEN_ID)
    assert (caught.value.status, caught.value.reason) == (404, None)


def test_a_body_that_is_not_json_still_becomes_an_api_error():
    def handler(request):
        return httpx.Response(400, text="<html>bad</html>")

    with pytest.raises(ApiError) as caught:
        _client(handler).principal()
    assert caught.value.status == 400
    assert caught.value.reason is None
    assert caught.value.detail


def test_a_connect_error_is_unreachable_with_status_0():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(Unreachable) as caught:
        _client(handler).principal()
    assert caught.value.status == 0
    assert "refused" in caught.value.detail


def test_a_5xx_is_unreachable():
    handler, _ = _recording(503, {"detail": "down"})
    with pytest.raises(Unreachable) as caught:
        _client(handler).principal()
    assert caught.value.status == 503


def test_the_ops_routes():
    handler, seen = _recording(
        200,
        {"v": 1, "kind": "x", "data": {"workspace_id": WS, "rows": []}, "error": None},
    )
    client = _client(handler)
    client.ops_story(WS, TOKEN_ID)
    client.ops_cards(WS, TOKEN_ID)
    client.ops_floating(WS)
    client.ops_floating(WS, limit=5)
    client.ops_account(WS, "a/b")
    client.ops_jobs(WS, "2026-09-15T12:00:00Z")
    client.ops_outbox(WS, "2026-09-15T12:00:00Z")
    client.ops_burst(WS, "2026-09-15T12:00:00Z")
    client.ops_posture()
    assert [
        (
            r.method,
            str(r.url.raw_path, "ascii").split("?")[0],
            str(r.url.query, "ascii"),
        )
        for r in seen
    ] == [
        ("GET", f"/api/v1/ops/workspaces/{WS}/story/{TOKEN_ID}", ""),
        ("GET", f"/api/v1/ops/workspaces/{WS}/cards/{TOKEN_ID}", ""),
        ("GET", f"/api/v1/ops/workspaces/{WS}/floating", ""),
        ("GET", f"/api/v1/ops/workspaces/{WS}/floating", "limit=5"),
        ("GET", f"/api/v1/ops/workspaces/{WS}/account/a%2Fb", ""),
        ("GET", f"/api/v1/ops/workspaces/{WS}/jobs", "since=2026-09-15T12%3A00%3A00Z"),
        (
            "GET",
            f"/api/v1/ops/workspaces/{WS}/outbox",
            "since=2026-09-15T12%3A00%3A00Z",
        ),
        ("GET", f"/api/v1/ops/workspaces/{WS}/burst", "since=2026-09-15T12%3A00%3A00Z"),
        ("GET", "/api/v1/ops/posture", ""),
    ]
