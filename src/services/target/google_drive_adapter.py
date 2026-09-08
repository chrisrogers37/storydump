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
import uuid
from dataclasses import dataclass
import logging
from typing import Any, Mapping, Optional, Protocol
from urllib.parse import urlencode

import httpx

from src.services.target import egress
from src.services.target.egress import EgressPolicy
from dataclasses import replace as _replace

from src.services.target.drive_adapter import (
    DriveMediaGone,
    DriveMediaTooLarge,
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
#: The most folders one WALK will queue (ruling 2026-09-08: the tree is walked
#: to any depth, lazily); past it the rest is skipped and the cut is said.
FOLDER_WALK_CAP = 2000


#: The picker's second root: folders shared TO the connected account, which
#: have no `root` parent and would otherwise be unreachable now that the
#: paste-a-link form is gone (review of #1246). A sentinel, not a Drive id.
SHARED_ROOT = "shared-with-me"


def is_folder_id(value: object) -> bool:
    return isinstance(value, str) and FOLDER_ID_RE.fullmatch(value) is not None


def _cursor_folder(entry: Mapping[str, Any]) -> dict:
    """A queue/current entry read back from a stored cursor, with every field
    the walk reads present: a missing one gets its default (the entry's own id
    as `top`; an empty path) rather than a KeyError the retry ladder would
    repeat forever."""
    fid = str(entry["id"])
    return {
        "id": fid,
        "name": entry.get("name"),
        "top": entry.get("top") or fid,
        "top_name": entry.get("top_name"),
        "path": entry.get("path") or "",
        "listed": bool(entry.get("listed")),
    }


@dataclass(frozen=True)
class FolderPage:
    """What the browser returns: the folders, and whether the cap cut them."""

    folders: list[dict]
    truncated: bool = False


#: Telegram's bot-upload limits, which bound the approval card's media fetch
#: (owner, 2026-09-08 — the card is the photo, as it was in the legacy product).
MEDIA_CARD_MAX_BYTES = {"image": 10 * 1024 * 1024, "video": 50 * 1024 * 1024}

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
    if not is_folder_id(config.get("folder_ref")):
        # The id is spliced into a Drive `q` string; a value outside the id
        # shape is refused here rather than trusted there (review of #1256).
        raise DriveTerminalError("folder_ref is not a Drive folder id")
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

        THE WALK (owner ruling 2026-09-08 — sources are the groups): every
        folder under the connected folder is walked, to any depth, LAZILY. A
        folder is asked for its subfolders once, when it is popped, and they
        join the queue carrying the top-level folder they sit under; then the
        folder's media is paged. A file's `category` is that TOP-LEVEL folder's
        name (None for the root's own files — a label the sync stores, never a
        key) and `folder_path` is its folder's path under the root. The cursor
        rides the checkpoint so a walk of any size is resumable:

            {"v": 2, "walk": <token>,
             "seen": <folders queued so far>, "truncated": <cap hit>,
             "current": {"id", "name", "top", "top_name", "path", "listed"},
             "queue":   [ … the same shape … ],   # folders still to list
             "page_token": <within current>}       # only while more pages

        `walk` is minted by the SYNC for each walk and carried through unchanged
        (a cursor handed in without one — the first call of a walk from a caller
        that did not mint — gets a fresh token here). A pre-v2 cursor with a
        `current` (the one-level walk of 2026-09-06, in flight at deploy) is
        ignored and the walk starts over. The complete checkpoint is
        `{"v": 2, "walk": …}` with at most `truncated` beside it: no
        `current`, `queue` or `page_token` (`checkpoint_incomplete` is the one
        definition the sync's chain reads), and the bare `page_token` shape is
        never emitted.

        `FOLDER_WALK_CAP` bounds the folders queued per walk; past it the rest
        is skipped and the cut is SAID (one warning) and carried to completion
        as `truncated`, never absorbed.
        """
        _refuse_unsupported_config(config)
        cp = dict(checkpoint or {})
        root = str(config["folder_ref"])
        box = _TokenBox(
            await self._token_provider(source_id, workspace_id=workspace_id)
        )

        walk = cp.get("walk")
        if not (cp.get("v") == 2 and isinstance(walk, str) and walk):
            walk = None
        resumed = cp.get("current") if walk is not None else None
        if resumed and is_folder_id(resumed.get("id")):
            # Healed, not trusted: a field a stored cursor lacks gets its
            # default rather than a KeyError that would retry forever.
            current: dict = _cursor_folder(resumed)
            queue = [
                _cursor_folder(f)
                for f in (cp.get("queue") or [])
                if is_folder_id((f or {}).get("id"))
            ]
            page_token = cp.get("page_token")
            seen = int(cp.get("seen") or 0)
            truncated = bool(cp.get("truncated"))
            visited = [v for v in (cp.get("visited") or []) if isinstance(v, str)]
        else:
            if cp.get("current") or cp.get("page_token"):
                logger.warning(
                    "drive source %s: the stored cursor cannot be resumed (pre-v2, or"
                    " malformed) — starting over",
                    source_id,
                )
            walk = walk or uuid.uuid4().hex
            current = {
                "id": root,
                "name": None,
                "top": root,
                "top_name": None,
                "path": "",
                "listed": False,
            }
            queue, page_token, seen, truncated, visited = [], None, 0, False, []

        def cursor(**extra: Any) -> dict:
            # `seen` and `truncated` ride to completion (the log reads them);
            # `visited` only while the walk is in flight.
            out: dict = {"v": 2, "walk": walk}
            if seen:
                out["seen"] = seen
            if truncated:
                out["truncated"] = True
            if extra:
                out["visited"] = visited
                out.update(extra)
            return out

        def advanced() -> dict:
            # The cursor past `current`: the next queued folder, or done.
            return cursor(current=queue[0], queue=queue[1:]) if queue else cursor()

        if not current.get("listed") and seen >= FOLDER_WALK_CAP:
            # Past the cap no listing is even asked for: every child would be
            # discarded, and a 2,000-folder tree would otherwise spend 2,000
            # requests per walk on nothing (review of #1256).
            if not truncated:
                logger.warning(
                    "drive source %s: more than %d folders under %s in one walk —"
                    " the rest are skipped this walk (truncated)",
                    source_id,
                    FOLDER_WALK_CAP,
                    root,
                )
                truncated = True
            current["listed"] = True
        if not current.get("listed"):
            # Lazy discovery: this folder's subfolders, once, as it is popped.
            try:
                children = await self._subfolders(
                    str(current["id"]),
                    source_id=source_id,
                    workspace_id=workspace_id,
                    box=box,
                )
            except DriveSourceGone:
                if current["id"] == root:
                    raise  # the connected folder itself is gone: the source's fault
                logger.warning(
                    "drive source %s: folder %r (%s) vanished before it was listed — skipped",
                    source_id,
                    current.get("path"),
                    current["id"],
                )
                return [], advanced()
            known = set(visited) | {str(current["id"])} | {str(f["id"]) for f in queue}
            for child in children:
                if child["id"] in known:
                    # Reachable by two paths (a multi-parent folder) or a cycle
                    # (a provider handing back an ancestor): walked once, and
                    # never spun to the cap (review of #1256).
                    logger.warning(
                        "drive source %s: folder %r (%s) reached twice under %s — skipped",
                        source_id,
                        child["name"],
                        child["id"],
                        current.get("path") or root,
                    )
                    continue
                if seen >= FOLDER_WALK_CAP:
                    if not truncated:
                        logger.warning(
                            "drive source %s: more than %d folders under %s in one walk —"
                            " the rest are skipped this walk (truncated)",
                            source_id,
                            FOLDER_WALK_CAP,
                            root,
                        )
                        truncated = True
                    break
                if current["id"] == root:
                    top, top_name, path = child["id"], child["name"], child["name"]
                else:
                    top, top_name = current["top"], current["top_name"]
                    path = f"{current['path']}/{child['name']}"
                queue.append(
                    {
                        "id": child["id"],
                        "name": child["name"],
                        "top": top,
                        "top_name": top_name,
                        "path": path,
                        "listed": False,
                    }
                )
                seen += 1
                known.add(child["id"])
            visited.append(str(current["id"]))
            current["listed"] = True

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

        try:
            payload = await self._get_as_workspace(
                f"{FILES_URL}?{urlencode(params)}",
                source_id=source_id,
                workspace_id=workspace_id,
                box=box,
            )
        except DriveSourceGone:
            if current["id"] == root:
                raise  # the connected folder itself is gone: the source's fault to report
            # A folder deleted or unshared mid-walk is not the source's fault
            # and must not wedge it: said once, skipped, the walk goes on
            # (review of #1251 — the stored cursor would otherwise point at
            # the dead folder forever).
            logger.warning(
                "drive source %s: folder %r (%s) is gone mid-walk — skipped",
                source_id,
                current.get("path"),
                current["id"],
            )
            return [], advanced()
        except DriveTerminalError:
            if not page_token:
                raise
            # An expired or invalid page token (a long stall between chunks):
            # restart THIS folder from its first page rather than fail the
            # source; the upsert is idempotent, so re-listing costs nothing.
            # `listed` rides along, so its subfolders are not queued twice.
            logger.warning(
                "drive source %s: page token for folder %r refused — restarting the folder",
                source_id,
                current.get("path"),
            )
            return [], cursor(current=current, queue=queue)

        category = current.get("top_name")
        items: list[dict] = []
        for entry in payload.get("files") or []:
            item = self._item_for(entry)
            if item is not None:
                if category is not None:
                    # Absent = the root's own files; the sync reads `.get`.
                    item["category"] = category
                item["folder_path"] = current.get("path") or ""
                items.append(item)

        next_token = payload.get("nextPageToken")
        if next_token:
            return items, cursor(current=current, queue=queue, page_token=next_token)
        return items, advanced()

    async def _subfolders(
        self,
        parent: str,
        *,
        source_id: str,
        workspace_id: str,
        box: Optional[_TokenBox] = None,
    ) -> list[dict]:
        """A folder's immediate subfolders in name order. Paged to
        `FOLDER_LIST_CAP` and the cut is SAID (one warning), never absorbed: a
        folder past the cap would otherwise quietly never sync. A provider
        handing the same page token back forever (a stub, or a Drive bug) ends
        the listing, said once."""
        params = {
            "q": _subfolder_query(parent),
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
                # folder that is really a file.
                if isinstance(fid, str) and not is_folder_id(fid):
                    # Spliced into a `q` string next: a shape outside the id
                    # alphabet is skipped and said, never trusted.
                    logger.warning(
                        "drive source %s: folder id %r under %s is not a Drive id — skipped",
                        source_id,
                        fid,
                        parent,
                    )
                    continue
                if (
                    isinstance(fid, str)
                    and isinstance(name, str)
                    and name.strip()
                    and entry.get("mimeType") == FOLDER_MIME
                ):
                    # Stripped: the label the sync stores is trimmed, as the
                    # mix service trims what it compares against.
                    folders.append({"id": fid, "name": name.strip()})
            page = payload.get("nextPageToken")
            if len(folders) > FOLDER_LIST_CAP or (
                len(folders) == FOLDER_LIST_CAP and page
            ):
                logger.warning(
                    "drive source %s: more than %d subfolders under %s — only the first"
                    " %d are walked; the rest never sync",
                    source_id,
                    FOLDER_LIST_CAP,
                    parent,
                    FOLDER_LIST_CAP,
                )
                return folders[:FOLDER_LIST_CAP]
            if not page:
                return folders

    async def fetch_bytes(
        self,
        *,
        source_id: str,
        workspace_id: str,
        file_ref: str,
        max_bytes: int,
    ) -> tuple[bytes, str, Optional[str]]:
        """A file's bytes, name and mime under the workspace grant — the
        approval card's media (owner, 2026-09-08). Metadata first, so a file
        over `max_bytes` is refused (`DriveMediaTooLarge`) before a download;
        a file that is gone is `DriveMediaGone`, never the source's fault. The
        download runs under the upload timeout class with the byte cap set to
        `max_bytes`, so a metadata size that lied cannot pull more."""
        if not is_folder_id(file_ref):  # file ids share the folder-id alphabet
            raise DriveTerminalError("file_ref is not a Drive id")
        box = _TokenBox(
            await self._token_provider(source_id, workspace_id=workspace_id)
        )
        meta_params = {"fields": "size,mimeType,name", "supportsAllDrives": "true"}
        try:
            meta = await self._get_as_workspace(
                f"{FILES_URL}/{file_ref}?{urlencode(meta_params)}",
                source_id=source_id,
                workspace_id=workspace_id,
                box=box,
            )
        except DriveSourceGone as exc:
            raise DriveMediaGone(
                f"drive file {file_ref} is gone for source {source_id}: {exc}"
            ) from exc
        size = int(meta.get("size") or 0)
        if size > max_bytes:
            raise DriveMediaTooLarge(
                f"drive file {file_ref} is {size} bytes; the cap is {max_bytes}"
            )
        media_params = {"alt": "media", "supportsAllDrives": "true"}
        policy = _replace(
            self._policy, timeout_class="upload", max_response_bytes=max_bytes
        )
        try:
            response = await self._fetch_as_workspace(
                f"{FILES_URL}/{file_ref}?{urlencode(media_params)}",
                source_id=source_id,
                workspace_id=workspace_id,
                box=box,
                policy=policy,
            )
        except DriveSourceGone as exc:
            raise DriveMediaGone(
                f"drive file {file_ref} vanished mid-fetch for source {source_id}: {exc}"
            ) from exc
        except egress.ResponseTooLarge as exc:
            raise DriveMediaTooLarge(str(exc)) from exc
        return response.content, str(meta.get("name") or file_ref), meta.get("mimeType")

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
        self,
        client: httpx.AsyncClient,
        url: str,
        token: str,
        *,
        policy: Optional[EgressPolicy] = None,
    ) -> httpx.Response:
        return await egress.request(
            client,
            "GET",
            url,
            policy=policy or self._policy,
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
        response = await self._fetch_as_workspace(
            url, source_id=source_id, workspace_id=workspace_id, box=box
        )
        try:
            return response.json()
        except ValueError as exc:
            raise DriveRetryableError(
                "drive answered 200 with a body that is not JSON"
            ) from exc

    async def _fetch_as_workspace(
        self,
        url: str,
        *,
        source_id: Optional[str],
        workspace_id: str,
        box: Optional[_TokenBox] = None,
        policy: Optional[EgressPolicy] = None,
    ) -> httpx.Response:
        """`_get_as_workspace` before the JSON step: the 200 response itself,
        for a body that is bytes (the card's media) — same token, same one
        re-mint, same status routing."""
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
            return await self._get_response(
                url, box.value, source_id=label, policy=policy
            )
        except DriveCredentialDead:
            box.value = await self._token_provider(
                source_id, workspace_id=workspace_id, fresh=True
            )
            return await self._get_response(
                url, box.value, source_id=label, policy=policy
            )

    async def _get(self, url: str, token: str, *, source_id: str) -> dict:
        """One floored GET whose 200 body is JSON (the listing calls)."""
        response = await self._get_response(url, token, source_id=source_id)
        try:
            return response.json()
        except ValueError as exc:
            raise DriveRetryableError(
                "drive answered 200 with a body that is not JSON"
            ) from exc

    async def _get_response(
        self,
        url: str,
        token: str,
        *,
        source_id: str,
        policy: Optional[EgressPolicy] = None,
    ) -> httpx.Response:
        """One floored GET, with the status mapped to the routing vocabulary;
        a 200 comes back whole for the caller to read."""
        try:
            if self._client is not None:
                response = await self._floored_get(
                    self._client, url, token, policy=policy
                )
            else:
                async with httpx.AsyncClient() as own:
                    response = await self._floored_get(own, url, token, policy=policy)
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
            return response

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
