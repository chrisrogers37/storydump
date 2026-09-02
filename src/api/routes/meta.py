"""Meta's two policy callbacks, as routes. Required for Meta App Review (#410).

Both doors verify a `signed_request` before doing anything at all, and both
refuse identically when verification fails. They then diverge, because Meta
treats them as different events and so do we:

* `POST /webhooks/meta/deauthorize` — revoke credentials. Deletes nothing.
* `POST /webhooks/meta/data-deletion` — record a receipt and return it.
  **Deletes nothing synchronously**; see the module docstring in
  `services/target/meta_callbacks.py` and the runbook for why.
* `GET  /webhooks/meta/data-deletion/status` — what Meta's returned URL points
  at, so a person can check a request they made.

Neither write door is registered with Meta yet. Registering the URLs is a
submission step, not a deploy step, and it is deliberately not done here.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request

from src.api.principal import require_engine
from src.services.target import meta_callbacks
from src.utils.logger import logger

router = APIRouter(tags=["meta"])


def _verified_subject(signed_request: str | None) -> tuple[str, str]:
    """Verify, then name the subject. Raises 400 for anything that fails.

    **One refusal for every failure mode.** A caller learns only that the
    request was not accepted — never whether the deployment holds a secret,
    whether the signature was close, or whether the subject exists here. The
    reason is logged for us and withheld from them.

    Returns the subject and the secret it verified against — see
    `verify_signed_request` for why the secret rides along rather than being
    re-read.
    """
    try:
        payload, secret = meta_callbacks.verify_signed_request(
            signed_request, meta_callbacks.app_secrets()
        )
    except meta_callbacks.SignedRequestInvalid as exc:
        logger.warning("meta callback rejected: %s", exc)
        raise HTTPException(status_code=400, detail="invalid signed_request")
    subject = meta_callbacks.subject_ref(payload)
    if subject is None:
        logger.warning("meta callback rejected: verified payload named no user_id")
        raise HTTPException(status_code=400, detail="invalid signed_request")
    return subject, secret


@router.post("/deauthorize")
async def deauthorize(request: Request, signed_request: str = Form(default="")):
    """A person removed the app. Stop using the credential; delete nothing.

    Meta has already invalidated the token on their side, so this is our half
    of the same fact rather than an independent decision. **The account, its
    media, its posting history and its workspace are untouched** — a disconnect
    is not a deletion request, and the one bug worth engineering against here
    is treating it as one.

    Answers 200 even when nothing matched. Meta retries non-2xx and there is
    nothing to retry: a subject we hold no credential for is a completed no-op.
    **A miss is logged as "not established" rather than "none"**, because
    `ig_accounts` is tenant-scoped under RLS and a tenant-less read returns
    zero rows whatever is stored — see `resolve_ig_accounts`.
    """
    subject, _ = _verified_subject(signed_request)
    engine = require_engine(request)
    async with engine.begin() as conn:
        accounts = await meta_callbacks.resolve_ig_accounts(conn, subject)
        revoked = await meta_callbacks.revoke_for_accounts(conn, accounts)
    logger.info(
        "meta deauthorize: %d account(s) visible, %d credential(s) revoked"
        " (a zero here is NOT established as 'no such account' — see RLS bound)",
        len(accounts),
        revoked,
    )
    return {"status": "ok"}


@router.post("/data-deletion")
async def data_deletion(request: Request, signed_request: str = Form(default="")):
    """Record a deletion request and return Meta's receipt. Deletes nothing.

    **This is deferred-with-receipt on purpose, and the shape is Meta's own.**
    The response contract is a `url` plus a `confirmation_code` precisely so
    the work can be asynchronous — returning a receipt is what the integration
    specifies, not a way around it.

    Three reasons it must not delete inline, in increasing order of weight:

    1. **The subject cannot be reliably identified.** Meta sends an app-scoped
       person id; the schema stores no Meta person, only an
       `ig_accounts.provider_account_ref` naming an ACCOUNT, sometimes a
       provisional `manual:<handle>`.
    2. **The blast radius is not the requester's to spend.** An Instagram
       account lives inside a workspace that may hold other members' content.
    3. **The product already has the right door, and it is deliberately not
       automatic.** `offboard_workspace` is owner-only, demands an explicit
       `confirm`, and runs a 30-day grace window. An unauthenticated external
       caller must not reach a stronger deletion than the owner's own
       confirmed one.

    **The receipt is logged, not stored, and that is a stated gap rather than
    a design preference.** `audit_events` is the natural home — no foreign key,
    outlives a workspace's cascade — but it is tenant-scoped under RLS
    (`p_audit_ins`/`p_audit_sel`) and a Meta callback carries no tenant, so a
    row could be neither written nor read back by confirmation code. Durable
    receipts need a tenant-free, indexed door — a named SECURITY DEFINER object
    in the shape of `fn_invitation_accept` — which is a migration and is filed
    rather than smuggled in here. Until then the log is the record, and the
    status endpoint does not pretend otherwise.
    """
    subject, secret = _verified_subject(signed_request)
    code = meta_callbacks.confirmation_code(subject, secret)
    logger.info("meta data-deletion requested, receipt %s", code)
    status_url = request.url_for("data_deletion_status")
    return {"url": f"{status_url}?code={code}", "confirmation_code": code}


@router.get("/data-deletion/status", name="data_deletion_status")
async def data_deletion_status(code: str = ""):
    """What Meta's returned URL points at. Read-only, and touches no database.

    Unauthenticated by necessity — a person follows this link from Meta holding
    nothing but the code.

    **It deliberately reports process rather than per-request state, because
    per-request state is not readable.** Looking a receipt up by code alone
    would need a tenant context the code cannot supply (`p_audit_sel`), so an
    endpoint that queried would return "not found" for every genuine receipt —
    strictly worse than saying plainly what is and is not known. Saying
    "completed" would be worse still: a claim nothing here can support.

    The code is shape-checked before it is echoed, so the page cannot be used
    to reflect arbitrary text back to a reader.
    """
    code = code.strip()
    if not code or len(code) != 16 or any(c not in "0123456789abcdef" for c in code):
        raise HTTPException(status_code=400, detail="malformed confirmation code")
    return {
        "code": code,
        "status": "received",
        "detail": (
            "Your deletion request was received. Instagram credentials linked"
            " to the identity it was issued for are revoked, so nothing further"
            " is posted or fetched. Removing a workspace and its contents is"
            " performed by that workspace's owner through a confirmed"
            " offboarding with a grace window, and is not carried out"
            " automatically from this request."
        ),
    }
