"""Meta's two policy callbacks, as routes. Required for Meta App Review.

Both doors verify a `signed_request` before doing anything else — reading no
more of the body than that one field needs (`_signed_request`) — and both
refuse identically when verification fails. They then diverge, because Meta
treats them as different events and so do we:

* `POST /webhooks/meta/deauthorize` — revoke credentials. Deletes nothing.
* `POST /webhooks/meta/data-deletion` — record a receipt and return it.
  **Deletes nothing synchronously**; see the module docstring in
  `services/target/meta_callbacks.py` and the runbook for why. With the
  archive's key ring configured the receipt is durable: a row behind a random
  capability, its cleanup done afterwards by the worker.
* `GET /webhooks/meta/data-deletion/status` — what Meta's returned URL points
  at, so a person can check a request they made: `in_progress` or `completed`.

Registering the URLs with Meta is a submission step, not a deploy step; this
code does not do it.
"""

from __future__ import annotations

from typing import NoReturn
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request, Response

from src.api.principal import BODY_TOO_LARGE_DETAIL, declared_length, require_engine
from src.services.target import archive_lifecycle, meta_callbacks
from src.utils.logger import logger

router = APIRouter(tags=["meta"])

#: The one body type Meta posts a callback as.
_FORM_TYPE = "application/x-www-form-urlencoded"
#: The most of a callback body that is read. Its one field, `signed_request`,
#: is a signature and a short base64url payload — a few hundred bytes.
SIGNED_REQUEST_MAX_BYTES = 16 * 1024
#: The most fields that body is parsed into; Meta sends one.
SIGNED_REQUEST_MAX_FIELDS = 8


def _too_large() -> NoReturn:
    """Refuse a callback body over `SIGNED_REQUEST_MAX_BYTES` (413)."""
    logger.warning(
        "meta callback rejected: body over %d bytes", SIGNED_REQUEST_MAX_BYTES
    )
    # The unread rest of the body is not drained: the connection ends.
    raise HTTPException(
        status_code=413,
        detail=BODY_TOO_LARGE_DETAIL,
        headers={"Connection": "close"},
    )


async def _signed_request(request: Request) -> str:
    """The `signed_request` field, read from a bounded urlencoded body.

    Nothing has been verified yet, so the body is read on these terms only:
    a type other than Meta's urlencoded form is refused (415) before a byte
    of it is read; a body over `SIGNED_REQUEST_MAX_BYTES` is refused (413) —
    by its declared length before reading, or as soon as the stream passes
    the cap — and only the bytes within the cap are buffered and parsed. A
    missing field reads as empty, which verification refuses like any other
    bad value.
    """
    media_type = request.headers.get("content-type", "").split(";", 1)[0]
    if media_type.strip().lower() != _FORM_TYPE:
        logger.warning("meta callback rejected: body is not an urlencoded form")
        raise HTTPException(status_code=415, detail="unsupported media type")
    declared = declared_length(request.scope["headers"])
    if declared is not None and declared > SIGNED_REQUEST_MAX_BYTES:
        _too_large()
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > SIGNED_REQUEST_MAX_BYTES:
            _too_large()
        body += chunk
    try:
        fields = parse_qs(
            body.decode("utf-8"),
            strict_parsing=True,
            max_num_fields=SIGNED_REQUEST_MAX_FIELDS,
        )
    except ValueError:  # UnicodeDecodeError included
        logger.warning("meta callback rejected: malformed form body")
        raise HTTPException(status_code=400, detail="invalid signed_request")
    # A repeated field answers with its last value.
    return fields.get("signed_request", [""])[-1]


def _verified_subject(
    signed_request: str | None,
) -> tuple[str, meta_callbacks.VerifiedRequest]:
    """Verify, then name the subject. Raises 400 for anything that fails.

    **One refusal for every failure mode.** A caller learns only that the
    request was not accepted — never whether the deployment holds a secret,
    whether the signature was close, or whether the subject exists here. The
    reason is logged for us and withheld from them.

    Returns the subject and the verification itself (`VerifiedRequest`): the
    payload, the secret it verified against, and the setting that held it, so
    no caller re-reads settings or re-verifies to learn which app signed.
    """
    try:
        verified = meta_callbacks.verify_request(signed_request)
    except meta_callbacks.SignedRequestInvalid as exc:
        logger.warning("meta callback rejected: %s", exc)
        raise HTTPException(status_code=400, detail="invalid signed_request")
    # The instrument for: which setting signed this — by name and
    # position, never by value. Which Meta app the URLs are registered under
    # is a dashboard fact; this line and one press of Meta's test button
    # answer it. The Instagram secret is tried first, then the Facebook one,
    # each only when it is set.
    names = meta_callbacks.app_secret_names()
    position = names.index(verified.setting_name)
    logger.info(
        "meta callback verified by %s (candidate %d of %d)",
        verified.setting_name,
        position + 1,
        len(names),
    )
    subject = meta_callbacks.subject_ref(verified.payload)
    if subject is None:
        logger.warning("meta callback rejected: verified payload named no user_id")
        raise HTTPException(status_code=400, detail="invalid signed_request")
    return subject, verified


@router.post("/deauthorize")
async def deauthorize(request: Request):
    """A person removed the app. Stop using what they authorized; delete nothing.

    Meta has already invalidated the token on their side, so this is our half
    of the same fact rather than an independent decision. **The account, its
    media, its posting history and its workspace are untouched** — a disconnect
    is not a deletion request, and the one bug worth engineering against here
    is treating it as one.

    The subject is matched through verified identity mappings (`meta_identity_mappings`):
    the (app, subject) a same-token OAuth exchange proved, never
    `provider_account_ref`, which names an account rather than the person
    Meta's id names. Each account's current authorization is revoked only when
    the request's order evidence puts it before the request, so a late
    callback never revokes a newer grant
    (`archive_lifecycle.revoke_subject_authorizations`).

    **For an account no mapping names, the account-reference lookup decides.**
    No mapping is recorded while the archive is not live, so the lookup
    (`resolve_ig_accounts`, then `revoke_for_accounts`) runs for every
    request. It skips only the (workspace, account) pairs a mapping names for
    the subject, whatever the mapped path decided about them (revoked, held,
    or left alone as newer): a mapping is evidence, and the account-reference
    lookup is the fallback for the accounts without any, never a second
    opinion on the rest. One workspace's mapping therefore never costs another
    workspace's unmapped account its revocation, and nothing is revoked twice.

    Answers 200 even when nothing matched. Meta retries non-2xx and there is
    nothing to retry: a subject we hold no credential for is a completed no-op.
    **A miss on the account-reference lookup is logged as "not established"
    rather than "none"**: the lookup reads every workspace through its door
    (fleet health functions), but the subject Meta sends names a person while
    `provider_account_ref` names an account — sometimes a provisional
    `manual:<handle>` no Meta id equals — see `resolve_ig_accounts`.
    """
    subject, verified = _verified_subject(await _signed_request(request))
    engine = require_engine(request)
    outcome = await archive_lifecycle.revoke_subject_authorizations(
        engine,
        app_identity=meta_callbacks.app_identity(verified.setting_name),
        subject=subject,
        issued_at=verified.payload.get("issued_at"),
    )
    if outcome.workspaces:
        logger.info(
            "meta deauthorize: %d workspace(s) mapped, %d authorization(s) revoked,"
            " %d left as ambiguous, %d newer left alone",
            outcome.workspaces,
            outcome.revoked,
            outcome.held,
            outcome.excluded,
        )
    async with engine.begin() as conn:
        accounts = [
            account
            for account in await meta_callbacks.resolve_ig_accounts(conn, subject)
            if (account.workspace_id, account.ig_account_id) not in outcome.mapped
        ]
        revoked = await meta_callbacks.revoke_for_accounts(conn, accounts)
    logger.info(
        "meta deauthorize: %d account(s) matched, %d credential(s) revoked"
        " (a zero here is NOT established as 'no such account': the subject"
        " names a person, the stored reference an account)",
        len(accounts),
        revoked,
    )
    return {"status": "ok"}


@router.post("/data-deletion")
async def data_deletion(request: Request):
    """Record a deletion request and return Meta's receipt. Deletes nothing here.

    **This is deferred-with-receipt on purpose, and the shape is Meta's own.**
    The response contract is a `url` plus a `confirmation_code` precisely so
    the work can be asynchronous — returning a receipt is what the integration
    specifies, not a way around it.

    Three reasons it must not delete inline, in increasing order of weight:

    1. **The subject cannot be reliably identified here.** Meta sends an
       app-scoped person id; which accounts it names is what the verified
       identity mappings record (`meta_identity_mappings`), and the request's scope is frozen
       against them inside the receipt door.
    2. **The blast radius is not the requester's to spend.** An Instagram
       account lives inside a workspace that may hold other members' content.
    3. **The product already has the right door, and it is deliberately not
       automatic.** `offboard_workspace` is owner-only, demands an explicit
       `confirm`, and runs a 30-day grace window. An unauthenticated external
       caller must not reach a stronger deletion than the owner's own
       confirmed one.

    **With the archive's key ring configured the receipt is durable**:
    a tenant-free door records the request under a keyed
    fingerprint of its signed payload — never the payload, the subject or a
    secret — freezes which authorizations are in scope, and the response
    carries a fresh random capability, of which the database keeps only the
    digest. An exact retry is the same receipt behind a new capability; a
    request made after a reconnect is a new one. The cleanup is the worker's
    and happens afterwards; the status endpoint reports it.

    **Without the key ring nothing changes:** the receipt is derived from the
    app secret, logged and not stored, and the status page says only that the
    request was received. A deletion request does not itself revoke the
    publishing credential (deauthorize does).

    A door failure is a 500, which Meta retries.
    """
    subject, verified = _verified_subject(await _signed_request(request))
    status_url = request.url_for("data_deletion_status")
    keys = getattr(request.app.state, "archive_keys", None)
    if keys is None:
        code = meta_callbacks.confirmation_code(subject, verified.secret)
        logger.info("meta data-deletion requested, receipt %s", code)
        return {"url": f"{status_url}?code={code}", "confirmation_code": code}
    if not meta_callbacks.RECEIPT_SUBJECT.fullmatch(subject):
        # A verified payload whose user_id is no app-scoped id is not a request
        # the door can record, and a 500 would have Meta retry it forever.
        logger.warning("meta data-deletion rejected: verified user_id is not an id")
        raise HTTPException(status_code=400, detail="invalid signed_request")
    app = meta_callbacks.app_identity(verified.setting_name)
    receipt = await meta_callbacks.receive_deletion(
        require_engine(request),
        app_identity=app,
        fingerprints=meta_callbacks.request_fingerprints(
            keys, app, verified.encoded_payload
        ),
        subject=subject,
        issued_at=verified.payload.get("issued_at"),
    )
    logger.info(
        "meta data-deletion recorded: receipt %s, %s, state %s",
        receipt.receipt_id,
        "replay" if receipt.replay else "new",
        receipt.state,
    )
    return {
        "url": f"{status_url}?code={receipt.code}",
        "confirmation_code": receipt.code,
    }


#: The status page is never cached: a receipt's answer changes as its cleanup does.
_NO_STORE = {"Cache-Control": "no-store"}
#: What the status page says, by what the receipt door reports. Fixed sentences:
#: nothing of the request, the subject or the accounts is echoed. None claims a
#: credential was revoked: a deletion request does not do that. Both states
#: record the limit that is not Storydump's to lift, and "completed" speaks only
#: of what Storydump itself holds: a remote erasure is never counted as done.
_CANNOT_WITHDRAW_DELIVERED = (
    "Storydump cannot withdraw copies that were already delivered to a Telegram"
    " chat, such as approval cards."
)
_OWNER_REMOVES_WORKSPACES = (
    "A workspace itself is removed only by its owner, through a confirmed"
    " offboarding with a grace window."
)
_STATUS_DETAIL = {
    "in_progress": (
        "Your deletion request was received and is being carried out: content"
        " imported from the Instagram accounts you authorized is removed from"
        " Storydump as that work finishes. "
        + _CANNOT_WITHDRAW_DELIVERED
        + " "
        + _OWNER_REMOVES_WORKSPACES
    ),
    "completed": (
        "Your deletion request was carried out: Storydump no longer holds content"
        " imported from the Instagram accounts you authorized. "
        + _CANNOT_WITHDRAW_DELIVERED
        + " "
        + _OWNER_REMOVES_WORKSPACES
    ),
}
#: The page for a deployment without the key ring: a receipt that was logged,
#: not stored, so the one thing it can say is that the request arrived.
_RECEIVED_DETAIL = (
    "Your deletion request was received. Removing a workspace and its contents"
    " is performed by that workspace's owner through a confirmed offboarding"
    " with a grace window, and is not carried out automatically from this"
    " request."
)


@router.get("/data-deletion/status", name="data_deletion_status")
async def data_deletion_status(request: Request, response: Response, code: str = ""):
    """What Meta's returned URL points at.

    Unauthenticated by necessity — a person follows this link from Meta holding
    nothing but the code, and the code is the whole of the credential: 128
    random bits whose digest is the lookup key. It answers `in_progress` or
    `completed`, nothing finer (a receipt held for review is still
    `in_progress`), never caches (`no-store`), and refuses the rest: 400 for a
    code of no known shape, 404 for a well-formed one nothing issued.

    A 16-hex code is the receipt a deployment without the key ring hands out.
    It was logged, not stored, so it is answered `received`, with nothing
    about its progress, without touching the database.

    The code is shape-checked before it is echoed, so the page cannot be used
    to reflect arbitrary text back to a reader.
    """
    code = code.strip()
    if meta_callbacks.CAPABILITY_CODE.fullmatch(code):
        state = await meta_callbacks.deletion_status(require_engine(request), code)
        if state is None:
            raise HTTPException(
                status_code=404,
                detail="unknown confirmation code",
                headers=_NO_STORE,
            )
        response.headers.update(_NO_STORE)
        return {"code": code, "status": state, "detail": _STATUS_DETAIL[state]}
    if meta_callbacks.LEGACY_CODE.fullmatch(code):
        response.headers.update(_NO_STORE)
        return {"code": code, "status": "received", "detail": _RECEIVED_DETAIL}
    raise HTTPException(status_code=400, detail="malformed confirmation code")
