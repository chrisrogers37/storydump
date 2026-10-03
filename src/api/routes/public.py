"""The public plane: routes a visitor reaches with no account (`07` §43).

`/api/v1` is the authenticated surface — every route there takes a session or
an allowlisted token, and `test_token_principal` holds that — so a route that
serves someone with no principal at all lives here, under `/public`, beside
`/auth`'s pre-authentication legs rather than inside the surface it would
break.

* `POST /public/waitlist` — add an address to the marketing waitlist. The
  landing site calls it server-side; it held a database credential of its own
  for this write until 100.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, Request
from starlette.requests import ClientDisconnect
from fastapi.responses import JSONResponse

from src.api.principal import (
    client_ip,
    parse_json_object,
    preauth_guard,
    require_engine,
)
from src.services.target import waitlist
from src.utils.logger import logger

router = APIRouter(tags=["public"])

#: Keys the waitlist's pre-auth counter apart from sign-in's.
WAITLIST_KEY_PREFIX = "waitlist:"
#: The caller is the landing site's server, so the counter's key is the site's
#: egress address and its limit is shared by every visitor the site forwards.
#: It is a ceiling on the table's growth, not a per-visitor limit, so it sits
#: well above `05`'s 30: at 30, one person sending 30 a minute would refuse
#: everyone. The per-visitor limit is the site's (a Vercel firewall rule on
#: `/api/waitlist`), where the visitor's address is known.
WAITLIST_LIMIT = 300
#: The largest body the route reads: an address and three campaign values fit
#: many times over. Anything larger is a 413 before it is parsed.
WAITLIST_MAX_BODY_BYTES = 8 * 1024
#: Waitlist requests one process serves at once. Each holds a pooled
#: connection and the counter row's lock, so a burst past this waits for a
#: slot instead of taking a connection, and the rest of the API keeps its pool.
WAITLIST_MAX_IN_FLIGHT = 4
#: Slots one client address may hold, so no single caller can fill them all
#: and turn the site's own requests away. Past this, its requests wait.
WAITLIST_MAX_PER_ADDRESS = 2
#: Requests one client address may have waiting or held at once; past this
#: it is a 429 at once, so one caller cannot pile up waiters.
WAITLIST_MAX_QUEUED_PER_ADDRESS = 10
#: How long a request waits for a free slot before the 429.
WAITLIST_SLOT_WAIT_SECONDS = 2.0


class _Share:
    """One address's part: its own slots, and how many of its requests hold
    or wait for one."""

    def __init__(self) -> None:
        self.slots = asyncio.Semaphore(WAITLIST_MAX_PER_ADDRESS)
        self.queued = 0


class WaitlistSlots:
    """The route's per-process concurrency: :data:`WAITLIST_MAX_IN_FLIGHT`
    slots, at most :data:`WAITLIST_MAX_PER_ADDRESS` held per address, and a
    FIFO wait of :data:`WAITLIST_SLOT_WAIT_SECONDS` for one. The site's server
    is one address, so a burst of signups waits its turn rather than being
    refused. ``create_app`` builds one per app, as ``app.state.waitlist_slots``."""

    def __init__(self) -> None:
        self.free = asyncio.Semaphore(WAITLIST_MAX_IN_FLIGHT)
        self.by_address: dict[str, _Share] = {}

    async def acquire(self, address: str) -> bool:
        share = self.by_address.get(address)
        if share is None:
            share = self.by_address[address] = _Share()
        if share.queued >= WAITLIST_MAX_QUEUED_PER_ADDRESS:
            return False
        share.queued += 1
        held: list[asyncio.Semaphore] = []

        async def take() -> None:
            # No await between an acquire and its append, so a timeout or a
            # cancel always finds in `held` exactly what was taken.
            for semaphore in (share.slots, self.free):
                await semaphore.acquire()
                held.append(semaphore)

        try:
            await asyncio.wait_for(take(), WAITLIST_SLOT_WAIT_SECONDS)
        except BaseException as exc:  # the timeout, or the request cancelled
            for semaphore in held:
                semaphore.release()
            self._drop(address)
            if isinstance(exc, asyncio.TimeoutError):
                return False
            raise
        return True

    def release(self, address: str) -> None:
        self.free.release()
        self.by_address[address].slots.release()
        self._drop(address)

    def _drop(self, address: str) -> None:
        share = self.by_address[address]
        share.queued -= 1
        if not share.queued:
            del self.by_address[address]


def _refusal(status: int, detail: str, reason: str) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"detail": detail, "reason": reason}
    )


async def _capped_body(request: Request) -> Optional[bytes]:
    """The body, or None past :data:`WAITLIST_MAX_BODY_BYTES` — judged by
    `Content-Length` first and by what arrives, so a chunked body is bounded
    too."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > WAITLIST_MAX_BODY_BYTES:
        return None
    body = bytearray()
    try:
        async for chunk in request.stream():
            body += chunk
            if len(body) > WAITLIST_MAX_BODY_BYTES:
                return None
    except ClientDisconnect:  # nobody is left to answer; no traceback for it
        return None
    return bytes(body)


@router.post("/waitlist", status_code=202)
async def join_waitlist(request: Request):
    """Add an address to the waitlist. The answer is the same whether the
    address is new or already there, so the route is no oracle for who has
    joined; an address the waitlist cannot hold is a 400 with
    `reason: invalid_email`.

    The one caller is the landing site's server. A browser always says where
    a request came from (`Origin` on a cross-site POST, `Sec-Fetch-Site` on
    every fetch) and a server-side fetch says neither, so a request carrying
    either is a page posting here directly and is refused before anything is
    read."""
    if "origin" in request.headers or "sec-fetch-site" in request.headers:
        return _refusal(403, "the waitlist takes no browser requests", "browser")
    media_type = request.headers.get("content-type", "").split(";")[0]
    if media_type.strip().lower() != "application/json":
        return _refusal(415, "the body must be application/json", "not_json")
    raw = await _capped_body(request)
    if raw is None:
        return _refusal(413, "the body is too large", "too_large")
    body = parse_json_object(raw)
    # A slot is taken only once the body is in hand, so a slow upload cannot
    # hold one.
    slots, address = request.app.state.waitlist_slots, client_ip(request)
    if not await slots.acquire(address):
        return _refusal(429, "too many waitlist requests", "busy")
    try:
        # The counter is spent before the body is judged, so a malformed or
        # refused request counts like any other.
        async with require_engine(request).begin() as conn:
            await preauth_guard(
                conn,
                request,
                detail="too many waitlist requests",
                key_prefix=WAITLIST_KEY_PREFIX,
                limit=WAITLIST_LIMIT,
            )
            if body is None:
                return _refusal(400, "the body must be a JSON object", "not_json")
            try:
                await waitlist.join(conn, body.get("email"), waitlist.campaign(body))
            except waitlist.InvalidWaitlistEmail:
                return _refusal(400, "not a valid email address", "invalid_email")
    finally:
        slots.release(address)
    logger.info("waitlist: an address was received")
    return {"status": "received"}
