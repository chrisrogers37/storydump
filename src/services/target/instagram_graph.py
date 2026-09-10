"""The real Meta adapter — Instagram content publishing over the Graph API
with an Instagram Login token (#1220 step 3, the publish leg).

Implements the seam `meta_adapter` declares (``create_container`` /
``container_status`` / ``publish`` / ``usage``) against
``graph.instagram.com`` — the host an Instagram-Login token is valid on (the
refresh leg, `credential_lifecycle`, speaks to the same host). Every call
runs under the egress floor (`egress.request`) and every answer is mapped to
the typed taxonomy the pipeline routes on:

- an ``error`` body → :func:`meta_adapter.classify_error` by code (9 → cap
  deferral, 9004 → terminal, anything else → retryable);
- a transport failure, a spent budget, an oversized body, a 5xx or a non-JSON
  body → :class:`MetaLostResponse` (the call may or may not have landed; the
  pipeline parks or repermits);
- the floor REFUSING to make the call (allowlist, private address, DNS) →
  :class:`MetaRetryableError` with code 0: nothing left the process, so the
  effect definitively did not happen and the ladder may retry — never a park;
- no token for the account (:class:`IgCredentialDead`) → the retryable Meta
  error with code 190 (OAuth), which the pipeline hands straight to a human;
- anything untyped propagates: a bug must look like a crash, not a retry.

**One attempt per effect.** The floor's default policy retries a transport
fault up to three times, which on ``media_publish`` re-POSTs the same
``creation_id`` after Meta may already have accepted it — a second story, or
a refusal reported as the outcome of a call that landed. The two POSTs run
with ``max_attempts=1``: a fault is a lost response, and R8's park plus the
reconciler's poll is the designed way to learn what happened. Reads may retry.

Stories only: ``media_type=STORIES`` with ``image_url`` or ``video_url`` (the
transit store's signed delivery URL, which Meta pulls); a story takes no
caption, so the seam's ``caption`` is accepted and ignored. The token rides
the ``Authorization: Bearer`` header — never a URL, never a body — and is
struck from any message anyway.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable, Optional

import httpx

from src.services.target import egress
from src.services.target.egress import (
    EgressBudgetExhausted,
    EgressPolicy,
    EgressRefused,
    ResponseTooLarge,
)
from src.services.target.ig_credentials import IgCredentialDead
from src.services.target.meta_adapter import (
    OAUTH_ERROR_CODE,
    MetaError,
    MetaLostResponse,
    MetaRetryableError,
    classify_error,
)

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.instagram.com"
DEFAULT_GRAPH_VERSION = "v21.0"

#: The effects: one attempt, so a transport fault is a lost response and never
#: a second POST of the same creation.
WRITE_POLICY = EgressPolicy(max_attempts=1)
#: The reads (status poll, quota): bounded short — the ambiguous-publish
#: reconciler polls inside its sweep's transaction (`work_loop`), so a slow
#: Meta must not hold that transaction for the floor's full 30 s budget.
READ_POLICY = EgressPolicy(total_budget_s=10.0, max_attempts=2)

#: A dead container's Meta-side reason is logged, so an ERROR carries its
#: cause (aspect ratio, unsupported format) somewhere an operator looks.
_DEAD = ("ERROR", "EXPIRED")

MediaKind = str
TokenReader = Callable[..., Awaitable[str]]


class InstagramGraphAdapter:
    def __init__(
        self,
        *,
        token_for_account: TokenReader,
        client: Optional[httpx.AsyncClient] = None,
        base: str = GRAPH_BASE,
        version: str = DEFAULT_GRAPH_VERSION,
        resolver=None,
        write_policy: EgressPolicy = WRITE_POLICY,
        read_policy: EgressPolicy = READ_POLICY,
    ):
        """*token_for_account* is ``async (provider_account_ref, *,
        workspace_id=None) -> str`` — `ig_credentials.token_for_account`
        bound to the engine in production."""
        self._token_for_account = token_for_account
        self._client = client or httpx.AsyncClient()
        self._base = base.rstrip("/")
        self._version = version.strip("/")
        # The floor's DNS pin; tests inject a static one so no name is looked up.
        self._resolver = resolver
        self._write_policy = write_policy
        self._read_policy = read_policy

    # -- the seam -----------------------------------------------------------

    async def create_container(
        self,
        provider_account_ref: str,
        *,
        media_url: str,
        media_kind: MediaKind,
        caption: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> str:
        """`POST /{ig-user-id}/media` — a STORIES container pulling the media
        from *media_url*. Returns the container id."""
        token = await self._token(provider_account_ref, workspace_id)
        data = {
            "media_type": "STORIES",
            ("video_url" if media_kind == "video" else "image_url"): media_url,
        }
        body = await self._call(
            "POST",
            f"{provider_account_ref}/media",
            token=token,
            data=data,
            policy=self._write_policy,
        )
        return self._id_of(body, "create_container")

    async def container_status(
        self,
        container_id: str,
        *,
        provider_account_ref: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> str:
        """`GET /{container-id}?fields=status_code,status` — FINISHED,
        IN_PROGRESS, ERROR, EXPIRED or PUBLISHED. The account is named so a
        resumed run (a fresh process, no memory of the create) can still find
        its token."""
        token = await self._token(provider_account_ref, workspace_id)
        body = await self._call(
            "GET",
            f"{container_id}",
            token=token,
            params={"fields": "status_code,status"},
            policy=self._read_policy,
        )
        status = body.get("status_code")
        if not isinstance(status, str) or not status:
            raise MetaLostResponse("container_status: answer carries no status_code")
        if status in _DEAD:
            logger.warning(
                "container %s is %s: %s", container_id, status, body.get("status")
            )
        return status

    async def publish(
        self,
        provider_account_ref: str,
        container_id: str,
        *,
        workspace_id: Optional[str] = None,
    ) -> str:
        """`POST /{ig-user-id}/media_publish` — the effect. Returns the media id."""
        token = await self._token(provider_account_ref, workspace_id)
        body = await self._call(
            "POST",
            f"{provider_account_ref}/media_publish",
            token=token,
            data={"creation_id": container_id},
            policy=self._write_policy,
        )
        return self._id_of(body, "publish")

    async def usage(
        self, provider_account_ref: str, *, workspace_id: Optional[str] = None
    ) -> dict:
        """`GET /{ig-user-id}/content_publishing_limit` — the rolling 24 h
        quota, `{"quota_usage", "quota_total"}`."""
        token = await self._token(provider_account_ref, workspace_id)
        body = await self._call(
            "GET",
            f"{provider_account_ref}/content_publishing_limit",
            token=token,
            params={"fields": "quota_usage,config"},
            policy=self._read_policy,
        )
        rows = body.get("data")
        first: dict = {}
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            first = rows[0]
        elif isinstance(rows, dict):
            first = rows
        config = first.get("config") if isinstance(first.get("config"), dict) else {}
        return {
            "quota_usage": _int(first.get("quota_usage")),
            "quota_total": _int(config.get("quota_total")),
        }

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- plumbing -----------------------------------------------------------

    async def _token(
        self, provider_account_ref: Optional[str], workspace_id: Optional[str]
    ) -> str:
        if not provider_account_ref:
            raise MetaRetryableError(
                code=OAUTH_ERROR_CODE, message="no account named for the token"
            )
        try:
            return await self._token_for_account(
                str(provider_account_ref), workspace_id=workspace_id
            )
        except IgCredentialDead as exc:
            raise MetaRetryableError(code=OAUTH_ERROR_CODE, message=str(exc)) from exc

    @staticmethod
    def _id_of(body: dict, method: str) -> str:
        value = body.get("id")
        if value is None or str(value) == "":
            raise MetaLostResponse(f"{method}: answer carries no id")
        return str(value)

    async def _call(
        self,
        method: str,
        path: str,
        *,
        token: str,
        policy: EgressPolicy,
        params: Optional[dict] = None,
        data: Optional[dict] = None,
    ) -> dict[str, Any]:
        url = f"{self._base}/{self._version}/{path}"
        kwargs: dict[str, Any] = {"headers": {"Authorization": f"Bearer {token}"}}
        if data is not None:
            kwargs["data"] = data
        if params is not None:
            kwargs["params"] = params
        if self._resolver is not None:
            kwargs["resolver"] = self._resolver
        where = f"{method} {path}"
        try:
            response = await egress.request(
                self._client, method, url, policy=policy, **kwargs
            )
        except EgressRefused as exc:
            # The floor would not make the call: nothing left the process, so
            # the effect definitively did not happen. Retryable, never a park.
            raise MetaRetryableError(
                code=0,
                message=f"{where}: egress refused: {self._redact(str(exc), token)}",
            ) from None
        except (EgressBudgetExhausted, ResponseTooLarge, httpx.HTTPError) as exc:
            raise MetaLostResponse(
                f"{where}: {type(exc).__name__}: {self._redact(str(exc), token)}"
            ) from None
        try:
            body = response.json()
        except (json.JSONDecodeError, ValueError):
            raise MetaLostResponse(
                f"{where}: non-JSON answer (HTTP {response.status_code})"
            ) from None
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            error = body["error"]
            code = _int(error.get("code"))
            subcode = error.get("error_subcode")
            message = self._redact(str(error.get("message") or ""), token)
            if response.status_code >= 500:
                raise MetaLostResponse(
                    f"{where}: HTTP {response.status_code} code={code}: {message}"
                )
            raise classify_error(code)(
                code=code,
                subcode=int(subcode) if isinstance(subcode, int) else None,
                message=message,
            )
        if response.status_code >= 500:
            raise MetaLostResponse(f"{where}: HTTP {response.status_code}")
        if not isinstance(body, dict):
            raise MetaLostResponse(f"{where}: answer is not an object")
        return body

    @staticmethod
    def _redact(text: str, token: str) -> str:
        return text.replace(token, "<TOKEN>") if token else text


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


__all__ = [
    "DEFAULT_GRAPH_VERSION",
    "GRAPH_BASE",
    "READ_POLICY",
    "WRITE_POLICY",
    "InstagramGraphAdapter",
    "MetaError",
    "OAUTH_ERROR_CODE",
]
