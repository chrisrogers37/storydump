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
- a transport failure, a 5xx or a non-JSON body → :class:`MetaLostResponse`
  (the call may or may not have landed; the pipeline parks or repermits);
- no token for the account (:class:`IgCredentialDead`) → the retryable Meta
  error with code 190 (OAuth), so the ladder retries and, exhausted, hands
  the intent to a human — exactly where a token Meta rejects would land.

Stories only: ``media_type=STORIES`` with ``image_url`` or ``video_url`` (the
transit store's signed delivery URL, which Meta pulls); a story takes no
caption, so the seam's ``caption`` is accepted and ignored. The token never
appears in a URL — it rides the form body — and is struck from any message.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable, Optional

import httpx

from src.services.target import egress
from src.services.target.egress import EgressPolicy
from src.services.target.ig_credentials import IgCredentialDead
from src.services.target.meta_adapter import (
    MetaError,
    MetaLostResponse,
    MetaRetryableError,
    classify_error,
)

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.instagram.com"
DEFAULT_GRAPH_VERSION = "v21.0"
#: Meta's OAuth error code — what a dead or absent token is reported as.
OAUTH_ERROR_CODE = 190

MediaKind = str


class InstagramGraphAdapter:
    def __init__(
        self,
        *,
        token_for_account: Callable[[str], Awaitable[str]],
        client: Optional[httpx.AsyncClient] = None,
        policy: Optional[EgressPolicy] = None,
        base: str = GRAPH_BASE,
        version: str = DEFAULT_GRAPH_VERSION,
        resolver=None,
    ):
        self._token_for_account = token_for_account
        self._client = client or httpx.AsyncClient()
        self._policy = policy or EgressPolicy()
        self._base = base.rstrip("/")
        self._version = version.strip("/")
        # The floor's DNS pin; tests inject a static one so no name is looked up.
        self._resolver = resolver

    # -- the seam -----------------------------------------------------------

    async def create_container(
        self,
        provider_account_ref: str,
        *,
        media_url: str,
        media_kind: MediaKind,
        caption: Optional[str] = None,
    ) -> str:
        """`POST /{ig-user-id}/media` — a STORIES container pulling the media
        from *media_url*. Returns the container id."""
        token = await self._token(provider_account_ref)
        data = {
            "media_type": "STORIES",
            ("video_url" if media_kind == "video" else "image_url"): media_url,
        }
        body = await self._call(
            "POST", f"{provider_account_ref}/media", token=token, data=data
        )
        return self._id_of(body, "create_container")

    async def container_status(
        self, container_id: str, *, provider_account_ref: Optional[str] = None
    ) -> str:
        """`GET /{container-id}?fields=status_code` — FINISHED, IN_PROGRESS,
        ERROR, EXPIRED or PUBLISHED. The account is named so a resumed run
        (a fresh process, no memory of the create) can still find its token."""
        token = await self._token(provider_account_ref)
        body = await self._call(
            "GET",
            f"{container_id}",
            token=token,
            params={"fields": "status_code,status"},
        )
        status = body.get("status_code")
        if not isinstance(status, str) or not status:
            raise MetaLostResponse("container_status: answer carries no status_code")
        return status

    async def publish(self, provider_account_ref: str, container_id: str) -> str:
        """`POST /{ig-user-id}/media_publish` — the effect. Returns the media id."""
        token = await self._token(provider_account_ref)
        body = await self._call(
            "POST",
            f"{provider_account_ref}/media_publish",
            token=token,
            data={"creation_id": container_id},
        )
        return self._id_of(body, "publish")

    async def usage(self, provider_account_ref: str) -> dict:
        """`GET /{ig-user-id}/content_publishing_limit` — the rolling 24 h
        quota, `{"quota_usage", "quota_total"}`."""
        token = await self._token(provider_account_ref)
        body = await self._call(
            "GET",
            f"{provider_account_ref}/content_publishing_limit",
            token=token,
            params={"fields": "quota_usage,config"},
        )
        rows = body.get("data") or []
        first = rows[0] if rows and isinstance(rows[0], dict) else {}
        config = first.get("config") if isinstance(first.get("config"), dict) else {}
        return {
            "quota_usage": int(first.get("quota_usage") or 0),
            "quota_total": int(config.get("quota_total") or 0),
        }

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- plumbing -----------------------------------------------------------

    async def _token(self, provider_account_ref: Optional[str]) -> str:
        if not provider_account_ref:
            raise MetaRetryableError(
                code=OAUTH_ERROR_CODE, message="no account named for the token"
            )
        try:
            return await self._token_for_account(str(provider_account_ref))
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
        params: Optional[dict] = None,
        data: Optional[dict] = None,
    ) -> dict[str, Any]:
        url = f"{self._base}/{self._version}/{path}"
        kwargs: dict[str, Any] = {}
        if data is not None:
            kwargs["data"] = {**data, "access_token": token}
        else:
            kwargs["params"] = {**(params or {}), "access_token": token}
        try:
            if self._resolver is not None:
                kwargs["resolver"] = self._resolver
            response = await egress.request(
                self._client, method, url, policy=self._policy, **kwargs
            )
        except Exception as exc:  # noqa: BLE001 — every transport path is "lost"
            raise MetaLostResponse(
                f"{method} {path}: transport failure: {self._redact(str(exc), token)}"
            ) from None
        try:
            body = response.json()
        except (json.JSONDecodeError, ValueError):
            raise MetaLostResponse(
                f"{method} {path}: non-JSON answer (HTTP {response.status_code})"
            ) from None
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            error = body["error"]
            try:
                code = int(error.get("code") or 0)
            except (TypeError, ValueError):
                code = 0
            subcode = error.get("error_subcode")
            message = self._redact(str(error.get("message") or ""), token)
            if response.status_code >= 500:
                raise MetaLostResponse(
                    f"{method} {path}: HTTP {response.status_code} code={code}: {message}"
                )
            raise classify_error(code)(
                code=code,
                subcode=int(subcode) if isinstance(subcode, int) else None,
                message=message,
            )
        if response.status_code >= 500:
            raise MetaLostResponse(f"{method} {path}: HTTP {response.status_code}")
        if not isinstance(body, dict):
            raise MetaLostResponse(f"{method} {path}: answer is not an object")
        return body

    @staticmethod
    def _redact(text: str, token: str) -> str:
        return text.replace(token, "<TOKEN>") if token else text


__all__ = [
    "DEFAULT_GRAPH_VERSION",
    "GRAPH_BASE",
    "InstagramGraphAdapter",
    "MetaError",
    "OAUTH_ERROR_CODE",
]
