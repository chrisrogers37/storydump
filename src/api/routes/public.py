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

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from src.api.principal import json_object, preauth_guard, require_engine
from src.services.target import waitlist
from src.utils.logger import logger

router = APIRouter(tags=["public"])

#: Keys the waitlist's pre-auth counter apart from sign-in's. The caller is
#: usually the landing site's server, so the key is its egress address and the
#: limit is shared by every visitor it forwards: the per-visitor limit is the
#: site's firewall rule on `/api/waitlist`.
WAITLIST_KEY_PREFIX = "waitlist:"


@router.post("/waitlist", status_code=202)
async def join_waitlist(request: Request):
    """Add an address to the waitlist. The answer is the same whether the
    address is new or already there, so the route is no oracle for who has
    joined; an address the waitlist cannot hold is a 400 with
    `reason: invalid_email`."""
    body = await json_object(request)
    async with require_engine(request).begin() as conn:
        await preauth_guard(
            conn,
            request,
            detail="too many waitlist requests",
            key_prefix=WAITLIST_KEY_PREFIX,
        )
        try:
            await waitlist.join(conn, body.get("email"), waitlist.campaign(body))
        except waitlist.InvalidWaitlistEmail:
            # Answered inside the block so the counter it spent commits.
            return JSONResponse(
                status_code=400,
                content={
                    "detail": "not a valid email address",
                    "reason": "invalid_email",
                },
            )
    logger.info("waitlist: an address was received")
    return {"status": "received"}
