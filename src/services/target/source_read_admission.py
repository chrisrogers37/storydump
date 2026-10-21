"""Current source-read authority and immutable request fences.

Reads do not depend on item eligibility or destination pool size. Captures are
internal trusted values, never a request body or an outward projection. The
composition root must inject activation; there is no production fallback.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.exceptions import StorydumpError
from src.services.target import jobs, readers, source_state
from src.services.target.instagram_sources import is_activated
from src.services.target.media_adapters import (
    SOURCE_FENCE_REASONS,
    MediaSourceFenceDrop,
)
from src.services.target.unit_of_work import unit_of_work


REASONS = SOURCE_FENCE_REASONS - {"source_fence_drop"}


class SourceReadRefused(StorydumpError):
    def __init__(self, reason: str, source_id: str):
        if reason not in REASONS:
            raise ValueError("unknown source read refusal")
        self.reason, self.source_id = reason, source_id
        super().__init__(reason)


@dataclass(frozen=True)
class SourceReadCapture:
    workspace_id: str
    source_id: str
    provider: str
    source_use_epoch: int
    generation_id: str | None = field(default=None, repr=False)
    credential_id: str | None = field(default=None, repr=False)
    account_ref: str | None = field(default=None, repr=False)

    def payload(self) -> dict:
        """Internal durable job capture; contains no secret/provider URL."""
        return dict(
            v=1,
            workspace_id=self.workspace_id,
            source_id=self.source_id,
            provider=self.provider,
            source_use_epoch=self.source_use_epoch,
            generation_id=self.generation_id,
            credential_id=self.credential_id,
            account_ref=self.account_ref,
        )


def capture_from_payload(payload: object) -> SourceReadCapture:
    if (
        not isinstance(payload, dict)
        or type(payload.get("v")) is not int
        or payload["v"] != 1
    ):
        raise SourceReadRefused("source_capture_missing", "")
    required = {
        "workspace_id",
        "source_id",
        "provider",
        "source_use_epoch",
        "generation_id",
        "credential_id",
        "account_ref",
    }
    if not required.issubset(payload) or type(payload["source_use_epoch"]) is not int:
        raise SourceReadRefused("source_capture_missing", "")
    c = SourceReadCapture(**{k: payload[k] for k in required})
    if c.provider != "instagram_past_posts" or any(
        not isinstance(v, str) or not v
        for v in (
            c.workspace_id,
            c.source_id,
            c.generation_id,
            c.credential_id,
            c.account_ref,
        )
    ):
        raise SourceReadRefused("source_capture_missing", c.source_id)
    return c


AUTHORITY_SQL = "SELECT * FROM fn_source_read_authority(CAST(:ws AS uuid),CAST(:sid AS uuid),CAST(:activated AS boolean))"


async def _lock(executor, ws):
    # All sources first so later composed writes cannot acquire an earlier
    # account/source after credential or generation locks.
    await source_state.lock_all_workspace_sources(executor, workspace_id=ws)
    await readers.rows(
        executor,
        "SELECT id FROM oauth_credentials WHERE workspace_id=:ws ORDER BY id FOR UPDATE",
        ws=ws,
    )
    await readers.rows(
        executor,
        "SELECT id FROM instagram_past_posts_generations WHERE workspace_id=:ws ORDER BY id FOR UPDATE",
        ws=ws,
    )


async def capture_source_read(executor, *, workspace_id, source_id, capability=None):
    ws, sid = str(workspace_id), str(source_id)
    await _lock(executor, ws)
    row = await readers.row(
        executor, AUTHORITY_SQL, ws=ws, sid=sid, activated=is_activated(capability)
    )
    if row is None:
        raise SourceReadRefused("source_not_found", sid)
    if not row["admitted"]:
        raise SourceReadRefused(row["reason"], sid)
    return SourceReadCapture(
        ws,
        sid,
        row["provider"],
        int(row["source_use_epoch"]),
        str(row["generation_id"]) if row["generation_id"] else None,
        str(row["credential_id"]) if row["credential_id"] else None,
        row["account_ref"],
    )


async def check_source_read(executor, capture, *, capability=None):
    if not isinstance(capture, SourceReadCapture):
        raise SourceReadRefused("source_capture_missing", "")
    current = await capture_source_read(
        executor,
        workspace_id=capture.workspace_id,
        source_id=capture.source_id,
        capability=capability,
    )
    if current.generation_id != capture.generation_id:
        raise SourceReadRefused("source_generation_changed", capture.source_id)
    if current.source_use_epoch != capture.source_use_epoch:
        raise SourceReadRefused("source_epoch_changed", capture.source_id)
    if (
        current.credential_id != capture.credential_id
        or current.account_ref != capture.account_ref
    ):
        raise SourceReadRefused("source_credential_changed", capture.source_id)
    return current


@dataclass(frozen=True)
class _Ticket:
    capture: SourceReadCapture = field(repr=False)
    owner: object = field(repr=False)


class _RequestAdmission:
    def __init__(self, engine, capture, capability, job, lock_timeout=None):
        self.engine, self.capture, self.capability, self.job = (
            engine,
            capture,
            capability,
            job,
        )
        self.lock_timeout = lock_timeout
        self.owner = object()

    async def _check(self):
        try:
            async with unit_of_work(
                self.engine,
                self.capture.workspace_id,
                actor_kind="worker",
                lock_timeout=self.lock_timeout,
            ).begin() as session:
                await check_source_read(
                    session, self.capture, capability=self.capability
                )
                if self.job is not None:
                    await jobs.assert_lease(
                        session, self.job["id"], self.job["lease_token"]
                    )
        except SourceReadRefused as exc:
            raise MediaSourceFenceDrop(reason=exc.reason) from None

    async def before_dispatch(self) -> object:
        await self._check()
        return _Ticket(self.capture, self.owner)

    async def after_return(self, ticket: object) -> None:
        if (
            not isinstance(ticket, _Ticket)
            or ticket.owner is not self.owner
            or ticket.capture != self.capture
        ):
            raise MediaSourceFenceDrop(reason="source_capture_mismatch")
        await self._check()


def admission_provider(engine, *, capability=None, job=None, lock_timeout=None):
    """A's mandatory provider; never refresh a stale access with a new capture.

    Each request's two checks take the source's full lock prefix. A caller that
    must not queue behind it (an explicit preview, which a person is waiting
    on) passes a *lock_timeout*: a check that waits longer raises the database's
    refusal instead of holding its connection. None, the default and the sync's,
    sets no bound.
    """

    async def provide(source_id: str, *, workspace_id: str, access):
        c = access.captured_authorization
        if not isinstance(c, SourceReadCapture) or c.provider != "instagram_past_posts":
            raise MediaSourceFenceDrop(reason="source_capture_missing")
        if (
            c.workspace_id != str(workspace_id)
            or c.source_id != str(source_id)
            or c.account_ref != access.account_ref
        ):
            raise MediaSourceFenceDrop(reason="source_capture_mismatch")
        return _RequestAdmission(engine, c, capability, job, lock_timeout)

    return provide
