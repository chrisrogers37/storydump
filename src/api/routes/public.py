"""The public plane: routes a visitor reaches with no account (`07` §43).

`/api/v1` is the authenticated surface — every route there takes a session or
an allowlisted token, and `test_token_principal` holds that — so a route that
serves someone with no principal at all lives here, under `/public`, beside
`/auth`'s pre-authentication legs rather than inside the surface it would
break.

* `POST /public/waitlist` — add an address to the marketing waitlist. The
  landing site calls it server-side; it held a database credential of its own
  for this write until 100. Each accepted address is also a Telegram message
  to each operator who has linked Telegram
  (`src/channels/telegram_waitlist_ping.py`), sent after the answer.
"""

from __future__ import annotations

import asyncio
import ipaddress
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from starlette.requests import ClientDisconnect
from fastapi.responses import JSONResponse

from src.api.principal import (
    client_ip,
    parse_json_object,
    preauth_guard,
    require_engine,
)
from src.config.settings import settings
from src.services.target import identity, waitlist
from src.services.target.webhook_ingress import verify_secret_token
from src.utils.logger import logger

router = APIRouter(tags=["public"])

#: Keys the waitlist's pre-auth counter apart from sign-in's.
WAITLIST_KEY_PREFIX = "waitlist:"
#: The caller is the landing site's server, so without the site's secret the
#: counter's key is the site's egress address and its limit is shared by every
#: visitor the site forwards. It is a ceiling on the table's growth, not a
#: per-visitor limit, so it sits well above `05`'s 30: at 30, one person
#: sending 30 a minute would refuse everyone.
WAITLIST_LIMIT = 300
#: With `WAITLIST_SITE_SECRET` set, the site proves it is the caller with the
#: secret and names the visitor it is forwarding, and each visitor gets a
#: counter and a slot share of their own: one script spends its own, not
#: everyone's.
SITE_SECRET_HEADER = "x-waitlist-site-secret"
VISITOR_IP_HEADER = "x-waitlist-visitor-ip"
WAITLIST_VISITOR_KEY_PREFIX = "waitlist:visitor:"
#: A visitor's own limit a minute: a person retrying a typo stays well under
#: it, and a household or office behind one address has room.
WAITLIST_VISITOR_LIMIT = 10
#: With the secret matched, every insert the waitlist accepts also spends one
#: counter shared by all visitors: a ceiling on the table's growth however the
#: visitor key is gamed (a leaked secret lets a caller name a fresh visitor
#: each time). Spent only once the insert has gone in, so refused or malformed
#: requests cannot fill it, and last, so its one row is locked only until the
#: commit that follows. Well above a launch peak, well below a flood.
WAITLIST_ACCEPTED_KEY = "waitlist:accepted"
WAITLIST_ACCEPTED_LIMIT = 600
TOO_MANY = "too many waitlist requests"
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


def _address(raw: Optional[str]) -> Optional[str]:
    """The key one client is counted under: an IPv4 address, or an IPv6
    address's /64 (one subscriber holds a whole /64, so keying on the address
    alone would hand a script 2^64 limits and slot shares). None when *raw* is
    not an address."""
    try:
        ip = ipaddress.ip_address((raw or "").strip())
    except ValueError:
        return None
    if ip.version == 4:
        return str(ip)
    if ip.ipv4_mapped:
        return str(ip.ipv4_mapped)
    return str(ipaddress.IPv6Network((ip.packed, 64), strict=False))


def _client(request: Request) -> Optional[tuple[str, str, int, bool]]:
    """Whose slot share and counter this request spends, as ``(key prefix,
    client, limit, capped)``, where *capped* (the secret matched) says an
    accepted insert also spends the all-visitors ceiling; None when the API holds the site's secret
    and the request does not carry it.

    No secret configured: the attributed peer and the shared counter, whatever
    the headers say, so the API answers as before the site starts sending
    them. Secret matched: the visitor the site names, at their own limit; a
    missing or malformed visitor address falls back to the peer and the shared
    counter rather than failing the signup. Every matched request is capped,
    the fallback too, or leaving out the visitor would skip the ceiling."""
    peer = client_ip(request)
    address = _address(peer) or peer
    expected = settings.waitlist_site_secret
    if not expected:
        return WAITLIST_KEY_PREFIX, address, WAITLIST_LIMIT, False
    if not verify_secret_token(request.headers.get(SITE_SECRET_HEADER), expected):
        return None
    visitor = _address(request.headers.get(VISITOR_IP_HEADER))
    if visitor is None:
        logger.warning("waitlist: the site sent no usable visitor address")
        return WAITLIST_KEY_PREFIX, address, WAITLIST_LIMIT, True
    return WAITLIST_VISITOR_KEY_PREFIX, visitor, WAITLIST_VISITOR_LIMIT, True


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
async def join_waitlist(request: Request, background: BackgroundTasks):
    """Add an address to the waitlist. The answer is the same whether the
    address is new or already there, so the route is no oracle for who has
    joined; an address the waitlist cannot hold is a 400 with
    `reason: invalid_email`.

    The one caller is the landing site's server. A browser always says where
    a request came from (`Origin` on a cross-site POST, `Sec-Fetch-Site` on
    every fetch) and a server-side fetch says neither, so a request carrying
    either is a page posting here directly and is refused before anything is
    read. With `WAITLIST_SITE_SECRET` set, so is a request without it.

    Every accepted address, a repeat too (the route cannot tell them apart),
    is a message to each operator who has linked Telegram, sent once the
    answer has gone."""
    if "origin" in request.headers or "sec-fetch-site" in request.headers:
        return _refusal(403, "the waitlist takes no browser requests", "browser")
    counted = _client(request)
    if counted is None:
        # Not logged here, where any caller could fill the log: the site logs
        # the refusal's reason when it is the caller.
        return _refusal(403, "the waitlist takes calls from the site only", "not_site")
    key_prefix, address, limit, capped = counted
    media_type = request.headers.get("content-type", "").split(";")[0]
    if media_type.strip().lower() != "application/json":
        return _refusal(415, "the body must be application/json", "not_json")
    raw = await _capped_body(request)
    if raw is None:
        return _refusal(413, "the body is too large", "too_large")
    body = parse_json_object(raw)
    # A slot is taken only once the body is in hand, so a slow upload cannot
    # hold one.
    slots = request.app.state.waitlist_slots
    if not await slots.acquire(address):
        return _refusal(429, TOO_MANY, "busy")
    try:
        # The counter is spent before the body is judged, so a malformed or
        # refused request counts like any other.
        async with require_engine(request).begin() as conn:
            await preauth_guard(
                conn,
                request,
                detail=TOO_MANY,
                key_prefix=key_prefix,
                limit=limit,
                client=address,
            )
            if body is None:
                return _refusal(400, "the body must be a JSON object", "not_json")
            # The insert and the ceiling's spend share a savepoint: past the
            # ceiling the 429 undoes both and keeps the caller's own spend
            # above, so one visitor's retries stay at their limit while the
            # ceiling is full.
            try:
                async with conn.begin_nested():
                    joined = await waitlist.join(
                        conn, body.get("email"), waitlist.campaign(body)
                    )
                    if capped:
                        await preauth_guard(
                            conn,
                            request,
                            detail=TOO_MANY,
                            key_prefix="",
                            limit=WAITLIST_ACCEPTED_LIMIT,
                            client=WAITLIST_ACCEPTED_KEY,
                        )
            except waitlist.InvalidWaitlistEmail:
                return _refusal(400, "not a valid email address", "invalid_email")
            except HTTPException as full:  # only the ceiling raises here
                return JSONResponse(
                    status_code=full.status_code, content={"detail": full.detail}
                )
            ping = request.app.state.waitlist_ping
            chats = (
                await identity.telegram_ids_for(conn, ping.operators) if ping else []
            )
    finally:
        slots.release(address)
    logger.info("waitlist: an address was received")
    if chats:
        # After the commit and after the answer: Telegram's pace never holds
        # the visitor, a slot or a connection, and the API's process runs the
        # task to its end.
        background.add_task(ping, joined, chats)
    elif ping is not None:
        logger.warning(
            "waitlist ping: not sent, no one in OPS_USER_IDS has linked Telegram"
        )
    return {"status": "received"}
