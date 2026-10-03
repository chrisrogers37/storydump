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

from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from src.api.principal import parse_json_object, preauth_guard, require_engine
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
#: connection and the counter row's lock, so a burst past this is a 429 before
#: it takes a connection, and the rest of the API keeps its pool.
WAITLIST_MAX_IN_FLIGHT = 4

_in_flight = 0


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
    async for chunk in request.stream():
        body += chunk
        if len(body) > WAITLIST_MAX_BODY_BYTES:
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
    global _in_flight
    if "origin" in request.headers or "sec-fetch-site" in request.headers:
        return _refusal(403, "the waitlist takes no browser requests", "browser")
    if request.headers.get("content-type", "").split(";")[0].strip() != (
        "application/json"
    ):
        return _refusal(415, "the body must be application/json", "not_json")
    raw = await _capped_body(request)
    if raw is None:
        return _refusal(413, "the body is too large", "too_large")
    body = parse_json_object(raw)
    # A slot is taken only once the body is in hand, so a slow upload cannot
    # hold one; there is no await between the check and the increment.
    if _in_flight >= WAITLIST_MAX_IN_FLIGHT:
        return _refusal(429, "too many waitlist requests", "busy")
    _in_flight += 1
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
        _in_flight -= 1
    logger.info("waitlist: an address was received")
    return {"status": "received"}
