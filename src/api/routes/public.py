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

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from src.api.principal import json_object, require_engine
from src.services.target import rate_counters, waitlist
from src.utils.logger import logger

router = APIRouter(tags=["public"])

#: `05`'s pre-auth admission scope and number, keyed apart from sign-in's
#: counters by a prefix. The caller is usually the landing site's server, so
#: the key is its egress address and the limit is shared by every visitor it
#: forwards; the site keeps its own per-visitor guard in front of it.
WAITLIST_SCOPE = "preauth_ip"
WAITLIST_KEY_PREFIX = "waitlist:"
WAITLIST_LIMIT = 30
WAITLIST_WINDOW_SECONDS = 60


def _client_ip(request: Request) -> str:
    """The attributed peer after ProxyHeadersMiddleware's trusted-proxy walk
    (#726/#765) — never a header read here (`auth._client_ip`)."""
    return request.client.host if request.client else "unknown"


@router.post("/waitlist", status_code=202)
async def join_waitlist(request: Request):
    """Add an address to the waitlist. The answer is the same whether the
    address is new or already there, so the route is no oracle for who has
    joined; an address the waitlist cannot hold is a 400 with
    `reason: invalid_email`."""
    body = await json_object(request)
    try:
        email = waitlist.normalize_email(body.get("email"))
    except waitlist.InvalidWaitlistEmail:
        return JSONResponse(
            status_code=400,
            content={"detail": "not a valid email address", "reason": "invalid_email"},
        )
    engine = require_engine(request)
    async with engine.begin() as conn:
        now = datetime.now(timezone.utc)
        count = await rate_counters.increment(
            conn,
            scope=WAITLIST_SCOPE,
            key=WAITLIST_KEY_PREFIX + _client_ip(request),
            window_start=rate_counters.window_start(now, WAITLIST_WINDOW_SECONDS),
            limit=WAITLIST_LIMIT,
        )
        if count is None:
            raise HTTPException(status_code=429, detail="too many waitlist requests")
        await waitlist.join(conn, email, waitlist.campaign(body))
    logger.info("waitlist: an address was received")
    return {"status": "received"}
