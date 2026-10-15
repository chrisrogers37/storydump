"""The owner's admission (#1723): `POST /api/v1/admissions`.

The one write among the owner's routes. It has a router of its own because
the `/ops` views are reads (`test_ops_routes.py` pins every one a GET), and it
is no command: the command route is keyed on a workspace, and an admission
belongs to none. The route is a thin wrapper over the core pathway,
`signup_admissions.admit`, which the ping's Admit tap calls too:

- **Who.** `require_ops`: a person named in `OPS_USER_IDS`, on a session or a
  person-bound token. The token write rule's workspace-less half
  (`principal.refuse_token_write`) comes first, so a service identity or a
  ``readonly`` token hears ``readonly_token`` before the body is read.
- **What.** The body is ``{email, resend?}``, which the route reads itself
  after both gates. The address travels in the body alone, never in the path
  or a query. Any address may be admitted, on the waitlist or not,
  and the table's CHECK decides what an address is.
- **The transaction.** One `engine.begin()` around `admit`, so the admission
  and its email's job commit or roll back together.
- **The answer.** :data:`STATUS` maps each outcome. The three that leave the
  address admitted are 200 ``{outcome}``, a re-run's ``admitted_before``
  included; the rest are refusals that name their reason.
- **The log.** One line per answer: the outcome and who asked (the person,
  and the token when it was one), never the address.
"""

from __future__ import annotations

from typing import Mapping

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from src.api import principal as principal_mod
from src.api.principal import (
    Principal,
    current_principal,
    refuse_token_write,
    require_engine,
    require_ops,
)
from src.config.settings import settings
from src.services.target import signup_admissions
from src.utils.logger import logger

router = APIRouter(tags=["admissions"])

#: `signup_admissions.admit`'s answer → its status, total over
#: `vocabulary.ADMIT_OUTCOMES` (pinned).
STATUS: Mapping[str, int] = {
    "admitted": 200,
    "resent": 200,
    "admitted_before": 200,
    # the spot is used: an account was created at or holds the address, or
    # its link was used
    "signed_up": 409,
    # an address the admissions table cannot hold
    "invalid_email": 400,
    # the deployment's, not the caller's: no web origin for the claim link
    "not_configured": 503,
}

#: What each refusal says; its reason is the outcome itself.
DETAIL: Mapping[str, str] = {
    "signed_up": "that address has already signed up",
    "invalid_email": "that is not an address an admission can hold",
    "not_configured": "the email's claim link cannot be built: set WEB_APP_URL",
}


async def _token_may_write(principal: Principal = Depends(current_principal)) -> None:
    """The token write rule as the route's first dependency, decided before
    `require_ops` and before the body: a service identity or a ``readonly``
    token is refused ``readonly_token``, whose fix is an operator token."""
    refuse_token_write(principal)


@router.post("/admissions", dependencies=[Depends(_token_may_write)])
async def admit(request: Request, principal: Principal = Depends(require_ops)):
    """Admit one address and queue its sign-up email, or, with ``resend``,
    queue the email again for an address admitted before. For the people in
    `OPS_USER_IDS`."""
    body = await principal_mod.json_object(request)
    resend = body.get("resend", False)
    if not isinstance(resend, bool):
        raise HTTPException(status_code=400, detail="resend must be true or false")
    async with require_engine(request).begin() as conn:
        outcome = await signup_admissions.admit(
            conn,
            email=body.get("email"),
            claim_base=signup_admissions.claim_base(settings.web_app_origin),
            resend=resend,
        )
    logger.info(
        "admission: %s, by user %s (token %s)",
        outcome,
        principal.user_id,
        principal.token_id or "none",
    )
    status = STATUS[outcome]
    if status == 200:
        return {"outcome": outcome}
    return JSONResponse(
        status_code=status, content={"detail": DETAIL[outcome], "reason": outcome}
    )
