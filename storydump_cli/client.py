"""The HTTP client: every read and write goes through the API with a bearer.

One prefix, one set of headers, one error type. ``ApiError`` carries the
status and the body's ``reason`` so the CLI maps exit codes without
parsing prose (``vocabulary.exit_code_for``); ``Unreachable`` — a connect
error, a timeout, a 5xx, or an answer that is not the API's JSON — is the
one failure that is not an answer. The ``transport`` argument exists for
tests (``httpx.MockTransport``); nothing else about the client changes
under test. A client is opened per request: a CLI makes one or two calls,
and a client closed deterministically is one that never warns at exit.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional
from urllib.parse import quote, urlparse

import httpx

from storydump_cli.config import ConfigError

from src.services.target.vocabulary import IDEMPOTENCY_HEADER
from storydump_cli import __version__

API_PREFIX = "/api/v1"


class ApiError(Exception):
    """The API answered with an error: *status*, the body's *reason* when it
    carried one (a refusal does — 403 token refusals, the command port's
    409/404 — while 401 and a tenant 404 say nothing), and its *detail*."""

    def __init__(self, status: int, reason: Optional[str], detail: str) -> None:
        suffix = f" ({reason})" if reason else ""
        super().__init__(f"{status}: {detail}{suffix}")
        self.status = status
        self.reason = reason
        self.detail = detail


class Unreachable(ApiError):
    """No usable answer: a transport failure (status 0) or a 5xx."""


def _segment(value: str) -> str:
    """A user-supplied id as one path segment, never more."""
    return quote(value, safe="")


#: Plain http is for a development server on this machine and nothing else.
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")


class InsecureApiUrl(ConfigError):
    """A bearer would travel in the clear to a host that is not this machine."""


def refuse_plain_http(base_url: str, *, allow: bool = False) -> None:
    """``https`` always; ``http`` only to a loopback host, or when the caller
    said so (``STORYDUMP_INSECURE_HTTP``). A missing or other scheme is
    refused the same way — the bearer is the whole credential."""
    parsed = urlparse(base_url)
    if parsed.scheme == "https" or allow:
        return
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "http" and (host in LOOPBACK_HOSTS or host.startswith("127.")):
        return
    raise InsecureApiUrl(
        f"refusing to send a token over plain http to {host or base_url!r}"
        " — use https, or set STORYDUMP_INSECURE_HTTP=1 for a development server"
    )


class Client:
    def __init__(
        self,
        base_url: str,
        token: Optional[str],
        transport: Optional[httpx.BaseTransport] = None,
        timeout: float = 10.0,
        allow_insecure_http: bool = False,
    ) -> None:
        refuse_plain_http(base_url, allow=allow_insecure_http)
        self.root_url = base_url.rstrip("/")
        self.base_url = self.root_url + API_PREFIX
        self.token = token
        self.transport = transport
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": f"storydump/{__version__}",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Mapping[str, Any]] = None,
        *,
        json_body: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
        root: bool = False,
    ) -> Any:
        """One request under `/api/v1` (or at the root for the health
        surfaces); the API's JSON, or the failure it answered."""
        try:
            with httpx.Client(
                base_url=self.root_url if root else self.base_url,
                headers={**self._headers(), **(headers or {})},
                transport=self.transport,
                timeout=self.timeout,
            ) as http:
                response = http.request(method, path, params=params, json=json_body)
        except httpx.TransportError as exc:
            raise Unreachable(0, None, f"{type(exc).__name__}: {exc}") from exc
        try:
            body: Any = response.json()
        except ValueError:
            body = None
        status = response.status_code
        if response.is_success:
            if body is None:
                raise Unreachable(status, None, "the answer was not the API's JSON")
            return body
        detail = body.get("detail") if isinstance(body, dict) else None
        if not isinstance(detail, str) or not detail:
            detail = f"HTTP {status} {response.reason_phrase}".strip()
        reason = body.get("reason") if isinstance(body, dict) else None
        if not isinstance(reason, str) or not reason:
            reason = None
        if status >= 500:
            raise Unreachable(status, reason, detail)
        raise ApiError(status, reason, detail)

    def principal(self) -> dict[str, Any]:
        return self._request("GET", "/me/principal")

    def command(
        self,
        workspace_id: str,
        command: str,
        args: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """One command through the port, under the caller's idempotency key —
        the same door a tap or a web click uses."""
        return self._request(
            "POST",
            f"/workspaces/{_segment(workspace_id)}/commands/{_segment(command)}",
            json_body=dict(args),
            headers={IDEMPOTENCY_HEADER: idempotency_key},
        )

    def health_api(self) -> dict[str, Any]:
        """`/health` alone — liveness, unauthenticated, at the root."""
        return self._request("GET", "/health", root=True)

    def health(self) -> dict[str, Any]:
        """The API's three health surfaces. `/health` must answer; the two
        dependency-touching surfaces may not (a 503 with no engine), and then
        the report carries that surface's error rather than losing the rest."""
        surfaces: dict[str, Any] = {"api": self.health_api()}
        for name, path in (
            ("scheduling", "/health/scheduling"),
            ("posting", "/health/posting"),
        ):
            try:
                surfaces[name] = self._request("GET", path, root=True)
            except (Unreachable, ApiError) as exc:
                surfaces[name] = {
                    "error": f"HTTP {exc.status}: {exc.detail}"
                    if exc.status
                    else exc.detail
                }
        return surfaces

    def list_my_tokens(self) -> dict[str, Any]:
        return self._request("GET", "/me/tokens")

    def revoke_my_token(self, token_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"/me/tokens/{_segment(token_id)}")

    def list_workspace_tokens(self, workspace_id: str) -> dict[str, Any]:
        return self._request("GET", f"/workspaces/{_segment(workspace_id)}/tokens")

    def revoke_workspace_token(
        self, workspace_id: str, token_id: str
    ) -> dict[str, Any]:
        return self._request(
            "DELETE",
            f"/workspaces/{_segment(workspace_id)}/tokens/{_segment(token_id)}",
        )

    # --- the read views (phase 02) ------------------------------------------
    #
    # Each answers for ONE workspace under ``/ops/workspaces/{ws}/…`` with the
    # phase-01 envelope; the CLI loops the principal's workspaces. ``since`` is
    # already an ISO-8601 UTC timestamp — the verb decides the window, the
    # client only carries it.

    def _ops(
        self,
        workspace_id: str,
        view: str,
        key: Optional[str] = None,
        params: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        path = f"/ops/workspaces/{_segment(workspace_id)}/{view}"
        if key is not None:
            path += f"/{_segment(key)}"
        return self._request("GET", path, params=params)

    def ops_story(self, workspace_id: str, intent_id: str) -> dict[str, Any]:
        return self._ops(workspace_id, "story", intent_id)

    def ops_cards(self, workspace_id: str, intent_id: str) -> dict[str, Any]:
        return self._ops(workspace_id, "cards", intent_id)

    def ops_floating(
        self, workspace_id: str, limit: Optional[int] = None
    ) -> dict[str, Any]:
        params = {"limit": limit} if limit is not None else None
        return self._ops(workspace_id, "floating", params=params)

    def ops_account(self, workspace_id: str, key: str) -> dict[str, Any]:
        return self._ops(workspace_id, "account", key)

    def ops_jobs(self, workspace_id: str, since: str) -> dict[str, Any]:
        return self._ops(workspace_id, "jobs", params={"since": since})

    def ops_outbox(self, workspace_id: str, since: str) -> dict[str, Any]:
        return self._ops(workspace_id, "outbox", params={"since": since})

    def ops_burst(self, workspace_id: str, since: str) -> dict[str, Any]:
        return self._ops(workspace_id, "burst", params={"since": since})

    def ops_posture(self) -> dict[str, Any]:
        return self._request("GET", "/ops/posture")
