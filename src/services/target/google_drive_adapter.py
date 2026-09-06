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

import re
from dataclasses import dataclass
import logging
from typing import Any, Mapping, Optional, Protocol
from urllib.parse import urlencode

import httpx

from src.services.target import egress
from src.services.target.drive_adapter import (
    DriveError,
    DriveLostResponse,
    DriveRetryableError,
    DriveTerminalError,
    ProbeResult,
    validate_source_config,
)
from src.services.target.media_sync import DriveCredentialDead, DriveSourceGone

logger = logging.getLogger(__name__)

#: Drive v3 listing endpoint. Host is already on the egress allowlist.
FILES_URL = "https://www.googleapis.com/drive/v3/files"
#: The folder browser's mime (#1165 lean (b)).
FOLDER_MIME = "application/vnd.google-apps.folder"
#: What a Drive id looks like. The browser splices `parent` into a `q`
#: string, so a value outside this shape is refused before any request —
#: not because Drive would be fooled, but because a query built from an
#: unchecked string is the class of bug that only fails later and elsewhere.
FOLDER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
#: The most folders the browser will page through for one parent.
FOLDER_LIST_CAP = 500


#: The picker's second root: folders shared TO the connected account, which
#: have no `root` parent and would otherwise be unreachable now that the
#: paste-a-link form is gone (review of #1246). A sentinel, not a Drive id.
SHARED_ROOT = "shared-with-me"


def is_folder_id(value: object) -> bool:
    return isinstance(value, str) and FOLDER_ID_RE.fullmatch(value) is not None


@dataclass(frozen=True)
class FolderPage:
    """What the browser returns: the folders, and whether the cap cut them."""

    folders: list[dict]
    truncated: bool = False


#: Requested per file. `md5Checksum` is the content hash without a download.
FILE_FIELDS = "id,name,mimeType,size,modifiedTime,md5Checksum"

#: One request's bound — never the traversal's. See the header.
DEFAULT_PAGE_SIZE = 200

#: mime prefix -> `media_items.media_kind`. Mirrors media_sync `_ALLOWED_KINDS`;
#: a kind absent here is never listed, so the executor never has to skip it.
_KIND_BY_PREFIX = (("image/", "image"), ("video/", "video"))


class _TokenBox:
    """The token one listing call is using. A re-mint inside `_get_as_workspace`
    replaces it here, so the NEXT request of the same call (the media page after
    the subfolder listing) does not resend the token Google just refused."""

    __slots__ = ("value",)

    def __init__(self, value: str) -> None:
        self.value = value


class TokenProvider(Protocol):
    """Resolves a usable Drive access token — the WORKSPACE's grant (069).

    `source_id` names the folder being synced, for the provider's own
    messages; it is `None` from the folder browser, which syncs nothing.
    `workspace_id` is the key: the credential is the workspace's, the read is
    bound to it by the WHERE and, where RLS is live, by the tenant GUC too.
    `fresh=True` asks for a re-minted token — the adapter's one retry after
    Google refused a token the door thought live (P5, #1247).
    """

    async def __call__(
        self, source_id: Optional[str], *, workspace_id: str, fresh: bool = False
    ) -> str: ...


def _kind_for(mime_type: str) -> Optional[str]:
    for prefix, kind in _KIND_BY_PREFIX:
        if mime_type.startswith(prefix):
            return kind
    return None


def _listing_query(folder_ref: str) -> str:
    """`q` for ONE folder's images and videos, trashed excluded. The walk
    (`list_changes`) calls it once per folder: the picked root, then each
    immediate subfolder, whose name is the items' category.

    `root_name` is deliberately NOT expressed here. It names a SUBFOLDER to
    scope the listing to, and a query that silently ignored it would list the
    parent — the exact wrong-place-looks-correct failure
    `validate_source_config` exists to refuse. A source carrying `root_name`
    is refused rather than mislisted.
    """
    kinds = " or ".join(f"mimeType contains '{p}'" for p, _ in _KIND_BY_PREFIX)
    return f"'{folder_ref}' in parents and trashed = false and ({kinds})"


def _subfolder_query(folder_ref: str) -> str:
    return (
        f"'{folder_ref}' in parents and mimeType = '{FOLDER_MIME}' and trashed = false"
    )


def _refuse_unsupported_config(config: Mapping[str, Any]) -> None:
    """Every refusal a config earns BEFORE a provider call, in one place.

    Shared by `list_changes` and `probe` deliberately. A probe that accepted a
    config the sync then refuses is worse than no probe: it green-lights a
    source into `active` that cannot list, and the failure surfaces later as a
    sync error nobody connects back to the connect form. The two must refuse
    the same set, which is a property that only holds if there is one set.
    """
    validate_source_config(config)
    if "root_name" in config and config["root_name"]:
        raise DriveTerminalError(
            "config carries root_name, which scopes listing to a subfolder"
            " this door cannot yet resolve. Refusing rather than listing"
            " the parent, which looks correct whether it comes back full"
            " or empty."
        )


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

        THE WALK (owner ruling 2026-09-06, legacy parity): the picked folder's
        own files first, then each immediate SUBFOLDER's files, and a file's
        category is the subfolder it sits in (None at the root). One level
        deep, as the legacy `google_drive_provider.list_files` was. The
        cursor rides the checkpoint so a walk of any size is resumable:

            {"v": 1,
             "current": {"id": <folder>, "name": <category or None>},
             "queue":   [{"id", "name"}, …],      # folders still to list
             "page_token": <within current>}        # only while more pages

        A call with no `current` STARTS the walk: one request lists the root's
        subfolders (name order; capped, and the cap said), then the root's
        first media page. Every later call lists ONE media page and advances
        the cursor; when the current folder is exhausted the next one is
        popped from the queue. The bare ``{"v": 1}`` — nothing pending — is
        the only statement that the walk is complete (`checkpoint_incomplete`
        is the one definition the sync's chain reads).

        Returns ``(items, checkpoint')``. `items` carry D37's canonical stable
        ref (the Drive file id, never a path) and `category`.
        """
        _refuse_unsupported_config(config)
        cp = dict(checkpoint or {})
        root = str(config["folder_ref"])
        box = _TokenBox(
            await self._token_provider(source_id, workspace_id=workspace_id)
        )
        if cp.get("current"):
            current: dict = dict(cp["current"])
            queue = [dict(f) for f in (cp.get("queue") or [])]
            page_token = cp.get("page_token")
        elif cp.get("page_token"):
            # The pre-walk shape, and the walk's own shape for a paged ROOT
            # with no subfolders left: more pages of the root, nothing queued.
            current, queue, page_token = (
                {"id": root, "name": None},
                [],
                cp["page_token"],
            )
        else:
            queue = await self._subfolders(
                root, source_id=source_id, workspace_id=workspace_id, box=box
            )
            current, page_token = {"id": root, "name": None}, None

        params = {
            "q": _listing_query(str(current["id"])),
            "fields": f"nextPageToken,files({FILE_FIELDS})",
            "pageSize": str(self._page_size),
            "spaces": "drive",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if page_token:
            params["pageToken"] = page_token

        def advanced() -> dict:
            # The cursor past `current`: the next queued folder, or done.
            return (
                {"v": 1, "current": queue[0], "queue": queue[1:]} if queue else {"v": 1}
            )

        try:
            payload = await self._get_as_workspace(
                f"{FILES_URL}?{urlencode(params)}",
                source_id=source_id,
                workspace_id=workspace_id,
                box=box,
            )
        except DriveSourceGone:
            if current["id"] == root:
                raise  # the picked folder itself is gone: the source's fault to report
            # A SUBFOLDER deleted or unshared mid-walk is not the source's fault
            # and must not wedge it: said once, skipped, the walk goes on
            # (review of #1251 — the stored cursor would otherwise point at
            # the dead folder forever).
            logger.warning(
                "drive source %s: subfolder %r (%s) is gone mid-walk — skipped",
                source_id,
                current.get("name"),
                current["id"],
            )
            return [], advanced()
        except DriveTerminalError:
            if not page_token:
                raise
            # An expired or invalid page token (a long stall between chunks):
            # restart THIS folder from its first page rather than fail the
            # source; the upsert is idempotent, so re-listing costs nothing.
            logger.warning(
                "drive source %s: page token for folder %r refused — restarting the folder",
                source_id,
                current.get("name"),
            )
            return [], {"v": 1, "current": current, "queue": queue}

        category = current.get("name")
        items: list[dict] = []
        for entry in payload.get("files") or []:
            item = self._item_for(entry)
            if item is not None:
                if category is not None:
                    # Absent = uncategorized (the root's own files); the sync
                    # reads `.get`, so the flat shape is unchanged for them.
                    item["category"] = category
                items.append(item)

        next_token = payload.get("nextPageToken")
        at_root_alone = current["id"] == root and not queue
        if next_token:
            if at_root_alone:
                return items, {"v": 1, "page_token": next_token}
            return items, {
                "v": 1,
                "current": current,
                "queue": queue,
                "page_token": next_token,
            }
        if queue:
            return items, {"v": 1, "current": queue[0], "queue": queue[1:]}
        return items, {"v": 1}

    async def _subfolders(
        self,
        root: str,
        *,
        source_id: str,
        workspace_id: str,
        box: Optional[_TokenBox] = None,
    ) -> list[dict]:
        """The root's immediate subfolders — the categories — in name order.
        Paged to `FOLDER_LIST_CAP` and the cut is SAID (one warning), never
        absorbed: a category past the cap would otherwise be a folder that
        quietly never syncs. A provider handing the same page token back
        forever (a stub, or a Drive bug) ends the listing, said once."""
        params = {
            "q": _subfolder_query(root),
            "fields": "nextPageToken,files(id,name,mimeType)",
            "pageSize": "200",
            "orderBy": "name_natural",
            "spaces": "drive",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        folders: list[dict] = []
        page: Optional[str] = None
        seen_tokens: set[str] = set()
        while True:
            if page:
                if page in seen_tokens:
                    logger.warning(
                        "drive source %s: subfolder listing repeated page token; stopping",
                        source_id,
                    )
                    return folders
                seen_tokens.add(page)
                params["pageToken"] = page
            payload = await self._get_as_workspace(
                f"{FILES_URL}?{urlencode(params)}",
                source_id=source_id,
                workspace_id=workspace_id,
                box=box,
            )
            for entry in payload.get("files") or []:
                fid, name = entry.get("id"), entry.get("name")
                # The query asks for folders; the mime is checked again because
                # a provider answering something else must not become a
                # "category" that is really a file.
                if (
                    isinstance(fid, str)
                    and isinstance(name, str)
                    and name.strip()
                    and entry.get("mimeType") == FOLDER_MIME
                ):
                    # Stripped: the mix service trims names, and a weight must
                    # match the category the sync writes.
                    folders.append({"id": fid, "name": name.strip()})
            page = payload.get("nextPageToken")
            if len(folders) > FOLDER_LIST_CAP or (
                len(folders) == FOLDER_LIST_CAP and page
            ):
                logger.warning(
                    "drive source %s: more than %d subfolders under %s — only the first"
                    " %d are categories; the rest never sync",
                    source_id,
                    FOLDER_LIST_CAP,
                    root,
                    FOLDER_LIST_CAP,
                )
                return folders[:FOLDER_LIST_CAP]
            if not page:
                return folders

    async def list_folders(
        self, *, parent: Optional[str], workspace_id: str
    ) -> FolderPage:
        """The folders under `parent` — the Drive root when None, the
        folders shared to the account when `SHARED_ROOT` — as ``{"id",
        "name"}`` rows in name order: the picker's read (#1165 lean (b)). The
        same grant and the same floored transport as `list_changes`; the token
        is the WORKSPACE's (069), so the provider is asked with no source.
        Pages are followed to `FOLDER_LIST_CAP`, and the page says when it
        was cut rather than looking complete.
        """
        if parent is not None and parent != SHARED_ROOT and not is_folder_id(parent):
            raise DriveTerminalError("parent is not a Drive folder id")
        scope = (
            "sharedWithMe = true"
            if parent == SHARED_ROOT
            else f"'{parent or 'root'}' in parents"
        )
        params = {
            "q": f"{scope} and mimeType = '{FOLDER_MIME}' and trashed = false",
            "fields": "nextPageToken,files(id,name)",
            "pageSize": "200",
            "orderBy": "name_natural",
            "spaces": "drive",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        folders: list[dict] = []
        page_token: Optional[str] = None
        while True:
            if page_token:
                params["pageToken"] = page_token
            payload = await self._get_as_workspace(
                f"{FILES_URL}?{urlencode(params)}",
                source_id=None,
                workspace_id=workspace_id,
            )
            for entry in payload.get("files") or []:
                fid, name = entry.get("id"), entry.get("name")
                if isinstance(fid, str) and isinstance(name, str):
                    folders.append({"id": fid, "name": name})
            page_token = payload.get("nextPageToken")
            if len(folders) > FOLDER_LIST_CAP:
                return FolderPage(folders[:FOLDER_LIST_CAP], truncated=True)
            if not page_token:
                return FolderPage(folders, truncated=False)
            if len(folders) == FOLDER_LIST_CAP:
                return FolderPage(folders, truncated=True)

    async def probe(
        self,
        config: Mapping[str, Any],
        *,
        source_id: str,
        workspace_id: str,
    ) -> ProbeResult:
        """`01`:78's third port verb — can this source be used, yes or why not.

        Connect/repair validation for `02` §2's `media_sources` state machine.
        Returns :class:`ProbeResult`; see its docstring for why a refusal is a
        result rather than a raise, and why the error class is the transport's
        own exception instead of a probe-specific vocabulary.

        **It exercises the door production actually uses.** The request is the
        same `files.list` `list_changes` issues, through the same
        `_refuse_unsupported_config` → token → `_get` path, with `pageSize=1`
        because the question is reachability rather than contents. A probe that
        asked a cheaper question — `files.get` on the folder id, say — would
        pass a config whose LISTING query is broken, which is the failure it
        exists to catch.

        **An EMPTY folder is `ok`.** Zero files is a legitimate answer from a
        reachable folder, and the state machine's question is whether the source
        can be listed, not whether anyone has put anything in it yet. Treating
        empty as a failure would refuse every correctly-configured new source.

        **`DriveLostResponse` is NOT converted to a result and propagates.** It
        means no answer exists, and the taxonomy is explicit that "we do not
        know" must not be catchable as "it failed" — collapsing it into
        ``ok=False`` would let a network blip flip a healthy source to `error`
        through a caller that reasonably branches on `ok`. A probe returns a
        verdict; when the provider never answered there is no verdict to return.

        **Today every gdrive source probes `DriveCredentialDead`**, because
        nothing writes a gdrive credential yet (`drive_credentials`). That is
        the honest reading of the current system, not a defect in this verb.
        """
        try:
            _refuse_unsupported_config(config)
            token = await self._token_provider(source_id, workspace_id=workspace_id)
            params = {
                "q": _listing_query(str(config["folder_ref"])),
                "fields": f"files({FILE_FIELDS})",
                "pageSize": "1",
                "spaces": "drive",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            await self._get(
                f"{FILES_URL}?{urlencode(params)}", token, source_id=source_id
            )
        except (DriveError, DriveSourceGone, DriveCredentialDead) as exc:
            # Deliberately NOT `except Exception`. An error outside the
            # taxonomy is a bug in this module, and the pipeline's discipline
            # is that a crashed run must look crashed rather than be reported
            # as a tidy negative verdict.
            return ProbeResult(ok=False, error=exc)
        return ProbeResult(ok=True)

    def _item_for(self, entry: Mapping[str, Any]) -> Optional[dict]:
        file_id = entry.get("id")
        if not file_id:
            logger.warning("drive listing returned a file with no id — skipped")
            return None
        mime = entry.get("mimeType") or ""
        kind = _kind_for(mime)
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
            # The provider's own content type, carried rather than consumed.
            # `kind` is the two-value `ck_media_kind` domain and is DERIVED from
            # this; deriving and then discarding the input leaves `mime_type`
            # — a column `054` defines and the media read already serves — NULL
            # for every provider-sourced item, recoverable only by re-listing
            # the folder. It is also the one classifier input that does not
            # depend on the file NAME, which for provider media is whatever
            # someone typed in Drive and need not carry an extension at all.
            "mime_type": mime,
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

    async def _get_as_workspace(
        self,
        url: str,
        *,
        source_id: Optional[str],
        workspace_id: str,
        box: Optional[_TokenBox] = None,
    ) -> dict:
        """One GET under the workspace's token, with ONE re-mint: a token the
        door handed out can die inside the request it was minted for, and
        Google can invalidate one early. On Google's 401/403 the provider is
        asked once more with `fresh=True` and the GET retried; a second
        refusal is the grant's, and propagates (P5, #1247). `box` lets a
        multi-request call (the walk) share one token and see the re-mint."""
        label = (
            source_id
            if source_id is not None
            else f"workspace {workspace_id} (folder browser)"
        )
        if box is None:
            box = _TokenBox(
                await self._token_provider(source_id, workspace_id=workspace_id)
            )
        try:
            return await self._get(url, box.value, source_id=label)
        except DriveCredentialDead:
            box.value = await self._token_provider(
                source_id, workspace_id=workspace_id, fresh=True
            )
            return await self._get(url, box.value, source_id=label)

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
