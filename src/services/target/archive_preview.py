"""Explicit archive previews: one bounded picture, admitted and fenced per request.

A preview is not the selected-post fetch (`instagram_preparation`): it reads one
picture for one request and keeps nothing. The thumbnail route asks for it only
through an injected port (`app.state.archive_preview`); by default none is injected,
so until a trusted composition root does, every archive thumbnail answers the
placeholder's 404.

One request, in this order:

1. Admission, in a short tenant transaction that commits before any provider
   call: the source's current read authority (`source_read_admission`: On,
   activated, a usable grant, the current generation), the item's observation
   in that generation, and the requested version equal to the pair the media
   reads advertise (`workspaces.ARCHIVE_THUMBNAIL_VERSION_SQL`).
2. The adapter's bounded lookup and download (`fetch_preview`), each request
   admitted again by the same per-call admission as every archive read.
3. A bounded local decode (:func:`preview_jpeg`); no network.
4. The fence, in a fresh short transaction: the capture is still current and
   the item still answers to the version. Only then are the bytes returned.

Every refusal is ``None`` with one log line naming its reason — never a URL, a
token or a Graph id. The version only stops a later request being answered
under an older pair; bytes already sent cannot be recalled.

Each admission and the fence take the source's whole lock prefix (the workspace,
every account, source, credential and generation row), up to eight times for one
picture, and a person is waiting on each. So the preview never queues behind it:
its own transactions and each per-request check wait at most
PREVIEW_LOCK_TIMEOUT for a lock, and a workspace holds at most
PREVIEW_SLOTS_PER_WORKSPACE previews in flight in this process, the rest
answering the placeholder at once, before they touch the database. A lock
timeout, a deadlock or any other database failure is that same placeholder.
"""

from __future__ import annotations

import asyncio
import io
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from PIL import Image
from sqlalchemy.exc import DBAPIError

from src.services.target import egress, readers, workspaces
from src.services.target import source_read_admission as admission
from src.services.target._dbapi import driver_candidates
from src.services.target.instagram_archive_adapter import PREVIEW_MAX_BYTES, PROVIDER
from src.services.target.instagram_sources import ArchiveCapability
from src.services.target.media_adapters import MEDIA_FAILURES
from src.services.target.unit_of_work import unit_of_work

logger = logging.getLogger(__name__)

#: The longest edge a preview may have; a larger picture is not a thumbnail.
PREVIEW_MAX_EDGE = 2048
#: The most scans a preview may hold. A progressive decode makes a pass per
#: scan, so this, with the edge, bounds its work: a baseline JPEG has one, a
#: progressive one about ten.
PREVIEW_MAX_SCANS = 100
#: How long one decode may run before the page's placeholder wins.
PREVIEW_DECODE_SECONDS = 2.0
#: How long a preview's transaction, or one of its per-request checks, waits for
#: a lock before the database refuses the wait: the tap's own bound
#: (`telegram_dispatch.TAP_LOCK_TIMEOUT`), and past it the page's placeholder
#: and an explicit retry, not a connection held in a queue.
PREVIEW_LOCK_TIMEOUT = "2s"
#: The previews one workspace may have in flight in this process, which is the
#: web's own two-request lane kept as a
#: server-side bound: several tabs, or a client that is not the web, cannot
#: queue on one workspace's locks and drain the pool.
PREVIEW_SLOTS_PER_WORKSPACE = 2


@dataclass(frozen=True)
class ArchivePreviewPorts:
    """What the trusted composition root supplies: the activation capability,
    and the adapter for a capture, handed the per-call admission to use."""

    capability: ArchiveCapability
    prepare_adapter: Callable[[admission.SourceReadCapture, Any], Awaitable[Any]]


class _Refused(Exception):
    """A preview this request may not have; the route answers 404."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


#: The item as the preview needs it, read with the media reads' own joins so
#: "has a current observation" means one thing: the parent to look up, the
#: asset its observation selected (this item's own slide) and the version.
_SUBJECT = (
    "SELECT mi.parent_media_id,"
    " ma.asset_media_id AS selected_asset_id,"
    f" {workspaces.ARCHIVE_THUMBNAIL_VERSION_SQL} AS thumbnail_version"
    "  FROM media_items m"
    f"{workspaces.thumbnail_joins('m.')}"
    " WHERE m.workspace_id = :ws AND m.id = :id AND ms.id = :sid"
    f"   AND ms.provider = '{PROVIDER}' AND ma.media_item_id IS NOT NULL"
    " AND mi.parent_media_id IS NOT NULL"
)


def preview_jpeg(data: bytes) -> bool:
    """Whether *data* is a complete JPEG inside the preview's bounds.

    Every bound holds before a pixel is decoded: the size, the signature, the
    scans — each opens with an SOS marker (FF DA) and entropy-coded data
    stuffs a 00 after every FF, so the count can only over-state them — and
    the dimensions, which Pillow reads from the header alone. The decode then
    covers at most PREVIEW_MAX_EDGE² pixels, and a truncated or corrupt image
    refuses. No network, nothing kept.
    """
    if (
        not isinstance(data, bytes)
        or not 0 < len(data) <= PREVIEW_MAX_BYTES
        or not data.startswith(b"\xff\xd8\xff")
        or data.count(b"\xff\xda") > PREVIEW_MAX_SCANS
    ):
        return False
    try:
        with Image.open(io.BytesIO(data), formats=["JPEG"]) as image:
            width, height = image.size
            if not (0 < width <= PREVIEW_MAX_EDGE and 0 < height <= PREVIEW_MAX_EDGE):
                return False
            image.load()
    except (
        OSError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        return False
    return True


async def _decodes(data: bytes) -> bool:
    """:func:`preview_jpeg` off the event loop, within PREVIEW_DECODE_SECONDS."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(preview_jpeg, data), PREVIEW_DECODE_SECONDS
        )
    except asyncio.TimeoutError:
        return False


def _reason(exc: BaseException) -> str:
    """What to log of a refusal or a failure: its closed reason, else the
    database's own class of failure (55P03 a lock wait past its timeout, 40P01 a
    deadlock), else the exception's class. Never its message, which may quote a
    provider URL or a statement."""
    reason = getattr(exc, "reason", None)
    if reason:
        return reason
    if isinstance(exc, DBAPIError):
        sqlstate = next(
            (
                c.sqlstate
                for c in driver_candidates(exc)
                if getattr(c, "sqlstate", None)
            ),
            "error",
        )
        return f"database_{sqlstate}"
    return type(exc).__name__


class ArchivePreview:
    """The thumbnail route's archive port."""

    def __init__(self, ports: ArchivePreviewPorts):
        self.ports = ports
        #: Previews in flight per workspace, in this process: an entry only
        #: while one is, so it never outgrows the workspaces being previewed.
        self._in_flight: dict[str, int] = {}

    async def preview(
        self, *, engine, workspace_id: str, media_id: str, version: str
    ) -> Optional[bytes]:
        """The item's preview bytes (a JPEG), or None when it may not have one.

        A workspace with PREVIEW_SLOTS_PER_WORKSPACE previews in flight is
        answered None here, before any statement or provider call: the check and
        the claim run with no `await` between them, so two requests cannot both
        take the last slot.
        """
        ws, mid = str(workspace_id), str(media_id)
        held = self._in_flight.get(ws, 0)
        if held >= PREVIEW_SLOTS_PER_WORKSPACE:
            logger.info(
                "archive preview of media %s not served: preview_slots_busy", mid
            )
            return None
        self._in_flight[ws] = held + 1
        try:
            return await self._serve(engine, ws, mid, version)
        finally:
            if self._in_flight[ws] > 1:
                self._in_flight[ws] -= 1
            else:
                del self._in_flight[ws]

    async def _serve(self, engine, ws: str, mid: str, version: str) -> Optional[bytes]:
        try:
            capture, subject = await self._admit(engine, ws, mid, version)
            adapter = await self.ports.prepare_adapter(
                capture,
                admission.admission_provider(
                    engine,
                    capability=self.ports.capability,
                    lock_timeout=PREVIEW_LOCK_TIMEOUT,
                ),
            )
            content = await adapter.fetch_preview(
                subject["parent_media_id"],
                source_id=capture.source_id,
                workspace_id=capture.workspace_id,
                selected_asset_id=subject["selected_asset_id"],
            )
            if not await _decodes(content):
                raise _Refused("preview_undecodable")
            await self._fence(engine, capture, mid, version)
        except (
            _Refused,
            admission.SourceReadRefused,
            egress.EgressRefused,
            egress.EgressBudgetExhausted,
            # Keep the floor refusal bounded even for an injected adapter
            # that has not translated it into a media failure.
            egress.ResponseTooLarge,
            # A lock wait past PREVIEW_LOCK_TIMEOUT, a deadlock, a dropped
            # connection: whichever transaction of the request met it.
            DBAPIError,
            *MEDIA_FAILURES,
        ) as exc:
            logger.info("archive preview of media %s not served: %s", mid, _reason(exc))
            return None
        return content

    async def _admit(self, engine, ws: str, mid: str, version: str):
        async with unit_of_work(
            engine, ws, actor_kind="system", lock_timeout=PREVIEW_LOCK_TIMEOUT
        ).begin() as conn:
            item = await readers.row(
                conn,
                "SELECT source_id FROM media_items WHERE workspace_id = :ws AND id = :id",
                ws=ws,
                id=mid,
            )
            if item is None:
                raise _Refused("media_not_found")
            # The source's locks come first; the observation is read under them.
            capture = await admission.capture_source_read(
                conn,
                workspace_id=ws,
                source_id=str(item["source_id"]),
                capability=self.ports.capability,
            )
            if capture.provider != PROVIDER:
                raise _Refused("not_archive")
            subject = await self._subject(conn, capture, mid)
            if subject["thumbnail_version"] != version:
                raise _Refused("stale_version")
            return capture, subject

    async def _fence(self, engine, capture, mid: str, version: str) -> None:
        async with unit_of_work(
            engine,
            capture.workspace_id,
            actor_kind="system",
            lock_timeout=PREVIEW_LOCK_TIMEOUT,
        ).begin() as conn:
            await admission.check_source_read(
                conn, capture, capability=self.ports.capability
            )
            subject = await self._subject(conn, capture, mid)
            if subject["thumbnail_version"] != version:
                raise _Refused("stale_version")

    @staticmethod
    async def _subject(conn, capture, mid: str) -> dict:
        subject = await readers.row(
            conn, _SUBJECT, ws=capture.workspace_id, id=mid, sid=capture.source_id
        )
        if subject is None:
            raise _Refused("no_current_observation")
        return subject
