"""The real Google Drive read leg — `01` :76's `list_changes` port (#982).

`drive_adapter.py` shipped the SEAM for #982 and a scripted stub, deliberately
without a real door ("building it now creates an untestable-until-M.3 path that
reads as coverage"). M.3 landed 2026-08-24, so that bound is spent and this is
the real door.

## It implements `list_changes`, NOT `list_files` — the seam is forked

The stub in `drive_adapter.py` provides ``list_files`` / ``fetch_bytes``. The
consumer whose two kinds are parked — :mod:`src.services.target.media_sync` —
calls ``deps.drive.list_changes`` and NOTHING else. Measured, not read:

    StubDriveAdapter.list_changes  present=False
    media_sync calls deps.drive.list_changes -> 1 site
    media_sync calls deps.drive.list_files   -> 0 sites

`01-target-architecture.md` :76 is normative and specifies
``list_changes(config, checkpoint) → (items, checkpoint')``; `media_sync` landed
2026-08-22 against it, `drive_adapter` landed 2026-08-21 against the other
shape. The doc and the live consumer agree, so this door is theirs. Wiring the
stub instead would unpark both kinds and then fail every job on ``AttributeError``
— a park counter falling is NOT evidence the leg works.

`drive_adapter.py` is reused rather than forked again: :func:`validate_source_config`
is explicitly "part of the seam contract, every implementation calls it first",
and its typed errors are the ladder's routing vocabulary.

## The port carries `source_id`, and it has to

`media_sources` has **no credential column**; `oauth_credentials` points AT the
source (`media_source_id`, exclusive per `ck_credentials_one_owner`). So the
credential is reachable only from the source's identity — which the doc's
two-argument form does not carry, and `config` (D37's `{v, folder_ref,
root_name?}`) does not either. Adding the id to `config` would fork the
ownership the schema already settled.

`source_id` is therefore keyword-only and additive: one call site moves, the
two-argument shape stays legible, and no existing caller of the port breaks.

## The token arrives injected — Drive has no refresh door yet

`credential_lifecycle` ships `ig_refresh` and nothing for Google, so a Drive
access token cannot yet be refreshed inside the tier. :class:`GoogleDriveAdapter`
takes an async ``token_provider(source_id) -> str`` instead of reading
`oauth_credentials` itself. That is the seam a Google refresh door lands behind,
and it keeps this module free of decryption and of the credential state machine.

A provider that raises :class:`DriveCredentialDead` routes to the source state
machine; anything else it raises rides the ladder unchanged.

## The bound is announced, never absorbed

Carried over from `drive_adapter`'s header, because the hazard is identical:
truncation and paging look the same to a caller. Exhaustion is structural — the
returned checkpoint carries ``page_token`` when more exist and omits it when the
listing is genuinely complete. `page_size` bounds ONE request; a bounded page is
always accompanied by a token, so the remainder is resumable rather than
dropped. There is no total cap.

## A file with no `md5Checksum` is SKIPPED AND SAID

`media_items.content_hash` is required and Drive supplies `md5Checksum` for
uploaded binary content — which every `image/*` and `video/*` file is. A file
that passes the mime filter and still carries no checksum is anomalous, and the
port has no channel for a partial item, so it is skipped. It is skipped
**loudly**, one warning naming the file id: a silent drop here is the same
data-loss-wearing-a-bound's-clothing this seam was designed against.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional, Protocol
from urllib.parse import urlencode

import httpx

from src.services.target import egress
from src.services.target.drive_adapter import (
    DriveLostResponse,
    DriveRetryableError,
    DriveTerminalError,
    validate_source_config,
)
from src.services.target.media_sync import DriveCredentialDead, DriveSourceGone

logger = logging.getLogger(__name__)

#: Drive v3 listing endpoint. Host is already on the egress allowlist.
FILES_URL = "https://www.googleapis.com/drive/v3/files"

#: Requested per file. `md5Checksum` is the content hash without a download.
FILE_FIELDS = "id,name,mimeType,size,modifiedTime,md5Checksum"

#: One request's bound — never the traversal's. See the header.
DEFAULT_PAGE_SIZE = 200

#: mime prefix -> `media_items.media_kind`. Mirrors media_sync `_ALLOWED_KINDS`;
#: a kind absent here is never listed, so the executor never has to skip it.
_KIND_BY_PREFIX = (("image/", "image"), ("video/", "video"))


class TokenProvider(Protocol):
    """Resolves a usable Drive access token for one media source.

    `workspace_id` is not redundant with `source_id`: `oauth_credentials` is
    tenant-scoped by RLS, so the read needs the workspace GUC applied or it
    returns nothing under `svc_worker` and an absent credential becomes
    indistinguishable from an unreadable one.
    """

    async def __call__(self, source_id: str, *, workspace_id: str) -> str: ...


def _kind_for(mime_type: str) -> Optional[str]:
    for prefix, kind in _KIND_BY_PREFIX:
        if mime_type.startswith(prefix):
            return kind
    return None


def _listing_query(config: Mapping[str, Any]) -> str:
    """`q` for one folder's images and videos, trashed excluded.

    `root_name` is deliberately NOT expressed here. It names a SUBFOLDER, whose
    resolution is a second round trip, and a query that silently ignored it
    would list the parent — the exact wrong-place-looks-correct failure
    `validate_source_config` exists to refuse. Until subfolder resolution
    lands, a source carrying `root_name` is refused rather than mislisted.
    """
    folder_ref = config["folder_ref"]
    kinds = " or ".join(f"mimeType contains '{p}'" for p, _ in _KIND_BY_PREFIX)
    return f"'{folder_ref}' in parents and trashed = false and ({kinds})"


class GoogleDriveAdapter:
    """`list_changes` against Drive v3, under the egress floor."""

    def __init__(
        self,
        *,
        token_provider: TokenProvider,
        client: Optional[httpx.AsyncClient] = None,
        policy: Optional[egress.EgressPolicy] = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> None:
        # `client` optional and per-call by default, following
        # `credential_lifecycle.ig_refresh` (:84-99) rather than inventing a
        # lifecycle: a long-lived client would need closing on shutdown, and
        # the sync cadence is minutes, so a per-call pool costs nothing. Tests
        # inject a transport here.
        self._client = client
        self._token_provider = token_provider
        self._policy = policy
        self._page_size = page_size

    async def list_changes(
        self,
        config: Mapping[str, Any],
        checkpoint: Optional[Mapping[str, Any]],
        *,
        source_id: str,
        workspace_id: str,
    ) -> tuple[list[dict], dict]:
        """One page of a source's media, plus the checkpoint that resumes it.

        Returns ``(items, checkpoint')``. `items` carry D37's canonical stable
        ref (the Drive file id, never a path). `checkpoint'` carries
        ``page_token`` only when the provider said more exist.
        """
        validate_source_config(config)
        if "root_name" in config and config["root_name"]:
            raise DriveTerminalError(
                "config carries root_name, which scopes listing to a subfolder"
                " this door cannot yet resolve. Refusing rather than listing"
                " the parent, which looks correct whether it comes back full"
                " or empty."
            )

        token = await self._token_provider(source_id, workspace_id=workspace_id)
        params = {
            "q": _listing_query(config),
            "fields": f"nextPageToken,files({FILE_FIELDS})",
            "pageSize": str(self._page_size),
            "spaces": "drive",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        page_token = (checkpoint or {}).get("page_token")
        if page_token:
            params["pageToken"] = page_token

        payload = await self._get(
            f"{FILES_URL}?{urlencode(params)}", token, source_id=source_id
        )

        items: list[dict] = []
        for entry in payload.get("files") or []:
            item = self._item_for(entry)
            if item is not None:
                items.append(item)

        next_token = payload.get("nextPageToken")
        checkpoint_out: dict = {"v": 1}
        if next_token:
            checkpoint_out["page_token"] = next_token
        return items, checkpoint_out

    def _item_for(self, entry: Mapping[str, Any]) -> Optional[dict]:
        file_id = entry.get("id")
        if not file_id:
            logger.warning("drive listing returned a file with no id — skipped")
            return None
        kind = _kind_for(entry.get("mimeType") or "")
        if kind is None:
            # The query already filters to image/video, so this is the provider
            # disagreeing with its own filter rather than an expected skip.
            logger.warning(
                "drive file %s has mime %r which the listing query excludes — skipped",
                file_id,
                entry.get("mimeType"),
            )
            return None
        content_hash = entry.get("md5Checksum")
        if not content_hash:
            logger.warning(
                "drive file %s (%s) carries no md5Checksum — skipped, because"
                " media_items.content_hash is required and this port has no"
                " channel for a partial item",
                file_id,
                entry.get("name"),
            )
            return None
        return {
            "ref": file_id,
            "name": entry.get("name"),
            "kind": kind,
            "content_hash": content_hash,
            "size_bytes": int(entry["size"]) if entry.get("size") else None,
            "modified_at": entry.get("modifiedTime"),
        }

    async def _floored_get(
        self, client: httpx.AsyncClient, url: str, token: str
    ) -> httpx.Response:
        return await egress.request(
            client,
            "GET",
            url,
            policy=self._policy,
            headers={"Authorization": f"Bearer {token}"},
        )

    async def _get(self, url: str, token: str, *, source_id: str) -> dict:
        """One floored GET, with the status mapped to the routing vocabulary."""
        try:
            if self._client is not None:
                response = await self._floored_get(self._client, url, token)
            else:
                async with httpx.AsyncClient() as own:
                    response = await self._floored_get(own, url, token)
        except egress.EgressBudgetExhausted as exc:
            # The floor retries transport failures internally and then raises
            # THIS rather than the original error, so a bare
            # `except httpx.TransportError` here can never fire. Budget
            # exhausted means every attempt failed to get an answer, which is
            # exactly what DriveLostResponse states.
            raise DriveLostResponse(f"drive gave no answer: {exc}") from exc
        except httpx.TransportError as exc:
            # No answer exists. Deliberately not a DriveError — "we do not
            # know" must not be catchable as "it failed".
            raise DriveLostResponse(f"drive transport died: {exc}") from exc

        status = response.status_code
        if status == 200:
            try:
                return response.json()
            except ValueError as exc:
                raise DriveRetryableError(
                    f"drive answered {status} with a body that is not JSON"
                ) from exc

        detail = _reason(response)
        if status in (401, 403):
            # 403 is overloaded: quota is retryable, a dead grant is not. The
            # reason string is the only discriminator Drive offers.
            if status == 403 and _is_quota(detail):
                raise DriveRetryableError(f"drive quota/rate limited: {detail}")
            raise DriveCredentialDead(
                f"drive refused the credential for source {source_id}"
                f" ({status}): {detail}"
            )
        if status == 404:
            raise DriveSourceGone(
                f"drive says the configured folder is gone for source"
                f" {source_id}: {detail}"
            )
        if status == 429 or status >= 500:
            raise DriveRetryableError(f"drive answered {status}: {detail}")
        raise DriveTerminalError(f"drive answered {status}: {detail}")


def _reason(response: httpx.Response) -> str:
    """Drive's error reason, or the raw body. Never raises."""
    try:
        body = response.json()
    except ValueError:
        return (response.text or "")[:200]
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error.get("status") or error)[:200]
    return str(body)[:200]


def _is_quota(detail: str) -> bool:
    lowered = detail.lower()
    return any(
        token in lowered
        for token in ("ratelimit", "rate limit", "quota", "userratelimitexceeded")
    )


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "FILE_FIELDS",
    "FILES_URL",
    "GoogleDriveAdapter",
    "TokenProvider",
]
