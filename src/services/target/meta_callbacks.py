"""Meta's two policy callbacks — `signed_request` verification and what we do with it.

Meta calls us in two situations that look similar and are not:

* **Deauthorize** — a person removed the app in their Facebook/Instagram
  settings. The credential is dead on Meta's side; ours should stop being used.
  **Nothing is deleted.**
* **Data deletion** — a person asked Meta to have their data deleted. Meta
  forwards the request and expects a *receipt*, not a completed deletion.

Conflating them would destroy tenant data on a mere disconnect, so the two
verbs live in separate functions here and separate routes above.

**Everything in this module fails closed.** The one thing an unauthenticated
caller must never be able to do is reach the side effects, so an absent app
secret refuses every request rather than accepting every request — the same
direction `webhook_ingress.verify_secret_token` already chose, and for the same
reason.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import text

from src.utils.logger import logger

#: Meta signs with HMAC-SHA256 and names the algorithm inside the payload. The
#: name is checked rather than trusted: accepting whatever the payload declares
#: is the classic algorithm-confusion hole, and the only value we will verify
#: is this one.
_EXPECTED_ALGORITHM = "HMAC-SHA256"


class SignedRequestInvalid(Exception):
    """The `signed_request` did not verify. Carries no detail for the caller.

    The reason is logged, never returned: an attacker probing the endpoint
    learns nothing about whether the deployment is armed, which key it holds,
    or how far their forgery got. Same reasoning as the Telegram route's
    deliberate refusal to distinguish "no secret configured" from "wrong
    secret".
    """


def _b64url_decode(segment: str) -> bytes:
    """Decode one base64url segment, restoring the padding Meta strips."""
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def parse_signed_request(
    signed_request: Optional[str], app_secret: Optional[str]
) -> dict[str, Any]:
    """Verify Meta's `signed_request` and return its payload.

    Raises `SignedRequestInvalid` for every failure mode. There is no partial
    success and no "probably fine" path.

    **The signature covers the RAW base64 payload string, not the decoded
    JSON.** Re-encoding the parsed object and signing that would produce a
    different byte string for the same logical payload (key order, separators),
    so the check would fail for honest requests and — worse — a lenient
    implementation that re-serialised could be steered by whitespace. The
    received bytes are what get verified.
    """
    if not app_secret:
        # A deployment with no secret cannot verify anything. Refusing is the
        # only safe answer: the alternative is an unauthenticated public door
        # onto a destructive operation.
        logger.warning("meta callback: refused, no FACEBOOK_APP_SECRET configured")
        raise SignedRequestInvalid("not configured")
    if not signed_request:
        raise SignedRequestInvalid("absent")

    parts = signed_request.split(".")
    if len(parts) != 2:
        raise SignedRequestInvalid("malformed")
    encoded_sig, encoded_payload = parts

    try:
        received_sig = _b64url_decode(encoded_sig)
        payload_bytes = _b64url_decode(encoded_payload)
    except (ValueError, UnicodeDecodeError):
        raise SignedRequestInvalid("undecodable")

    try:
        payload = json.loads(payload_bytes)
    except (ValueError, UnicodeDecodeError):
        raise SignedRequestInvalid("payload not json")
    if not isinstance(payload, dict):
        raise SignedRequestInvalid("payload not an object")

    # Checked BEFORE the comparison, and against a constant. A payload naming
    # any other algorithm is rejected outright rather than dispatched on.
    if payload.get("algorithm") != _EXPECTED_ALGORITHM:
        raise SignedRequestInvalid("unexpected algorithm")

    expected_sig = hmac.new(
        app_secret.encode("utf-8"),
        encoded_payload.encode("ascii"),
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(received_sig, expected_sig):
        raise SignedRequestInvalid("signature mismatch")

    return payload


def app_secrets() -> list[str]:
    """Every Meta app secret this deployment could legitimately be signed by.

    **Not a single setting, because picking one wrong fails App Review
    outright.** `settings` carries two — `INSTAGRAM_APP_SECRET` annotated
    *preferred* and `FACEBOOK_APP_SECRET` annotated *legacy* — and which one
    signs these callbacks is decided by which Meta app the URLs are registered
    under, which is a submission-time fact this code cannot read. Keying to
    the wrong one refuses 100% of Meta's requests, and the only symptom is a
    warning line among ordinary prober noise.

    So both are candidates, preferred first, and a request is accepted if it
    verifies against either. That is the ordinary key-rotation posture rather
    than a loosening: every candidate is a secret WE hold, each is checked by a
    full constant-time HMAC comparison, and an empty list — no secret
    configured at all — still refuses everything.
    """
    from src.config.settings import settings

    candidates = [settings.INSTAGRAM_APP_SECRET, settings.FACEBOOK_APP_SECRET]
    return [c for c in candidates if c]


def verify_signed_request(
    signed_request: Optional[str], secrets: list[str]
) -> tuple[dict[str, Any], str]:
    """`parse_signed_request` against each candidate secret; first match wins.

    Returns the payload **and the secret that verified it**, so a caller
    needing that secret afterwards (to derive a receipt code) takes it from the
    verification that just succeeded rather than re-reaching into settings. It
    is then impossible to write the `or ""` fallback that would mint a
    receipt keyed on an empty HMAC key — attacker-computable, and guarded only
    by a comment. The invariant is carried by the return type instead.

    An empty `secrets` refuses, exactly as a single absent secret does — the
    fail-closed direction is a property of having nothing to verify with, not
    of how many settings were consulted.
    """
    if not secrets:
        logger.warning("meta callback: refused, no Meta app secret configured")
        raise SignedRequestInvalid("not configured")
    last: Exception = SignedRequestInvalid("not configured")
    for secret in secrets:
        try:
            return parse_signed_request(signed_request, secret), secret
        except SignedRequestInvalid as exc:
            last = exc
    raise last


def subject_ref(payload: dict[str, Any]) -> Optional[str]:
    """The Meta-side identifier this callback is about, or None.

    Meta sends an **app-scoped** id. It is stable per (app, person) and is not
    the Instagram business-account id we store, which is why resolution here is
    best-effort by construction — see `resolve_ig_accounts`.
    """
    value = payload.get("user_id")
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def confirmation_code(subject: str, app_secret: str) -> str:
    """A stable, unguessable receipt id for a deletion request.

    Derived rather than random for two reasons. It is **idempotent** — Meta
    retries, and a retry must not mint a second receipt for one request — and
    it is **unguessable without the app secret**, so the status endpoint cannot
    be enumerated to learn which Meta identities this deployment knows.

    Truncated to 16 hex characters: long enough that guessing is hopeless,
    short enough to paste into Meta's form.
    """
    return hmac.new(
        app_secret.encode("utf-8"),
        f"meta-deletion:{subject}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:16]


@dataclass(frozen=True)
class AccountRef:
    """One `ig_accounts` row this callback plausibly refers to."""

    ig_account_id: str
    workspace_id: str


async def resolve_ig_accounts(conn, subject: str) -> list[AccountRef]:
    """`ig_accounts` rows whose provider reference equals *subject*.

    **Two independent reasons this usually returns nothing, and the second one
    is a hard bound rather than a property of the data.**

    First, the identifier may simply not match. The target schema stores no
    Meta person — `user_identities.provider` is CHECK-constrained to
    `('telegram','google')` and `oauth_credentials` is keyed to an
    `ig_account_id` — so the only Meta-shaped value held is
    `provider_account_ref`, which names an ACCOUNT rather than a person and is
    sometimes a provisional `manual:<handle>` no Meta id will ever equal.

    Second, and decisively: **`ig_accounts` is tenant-scoped under RLS**
    (`058_rls_and_policies.sql:266`, `p_tenant ... TO svc_ingress, svc_worker`
    `USING (workspace_id = current_setting('app.tenant_id'))`). A Meta callback
    names no workspace, so there is no tenant to set, and under either of those
    roles this SELECT returns **zero rows regardless of what is stored**.

    That matters more than it looks, because the two causes are
    indistinguishable here and they fail in the SAME reassuring direction: an
    empty list reads as "we hold nothing for this person" whether or not that
    is true. The caller therefore must not report a miss as a finding of fact —
    see `routes/meta.py`, which says "not established" rather than "none", and
    the runbook, which states the door this needs.
    """
    rows = (
        await conn.execute(
            text(
                "SELECT id, workspace_id FROM ig_accounts"
                " WHERE provider_account_ref = :ref"
            ),
            {"ref": subject},
        )
    ).fetchall()
    return [AccountRef(ig_account_id=str(r[0]), workspace_id=str(r[1])) for r in rows]


async def revoke_for_accounts(conn, accounts: list[AccountRef]) -> int:
    """Mark every credential for *accounts* revoked. Returns the row count.

    **This is the whole of what deauthorize does, deliberately.** Meta has
    already invalidated the token on their side; our job is to stop presenting
    it. The account row, its media, its history and its workspace are all
    untouched — a person disconnecting an integration has not asked us to
    delete anything, and treating a disconnect as a deletion is the failure
    this separation exists to prevent.

    **Scoped on the `(ig_account_id, workspace_id)` PAIR, not the account
    alone, and that is load-bearing rather than tidy.** No migration declares
    `FORCE ROW LEVEL SECURITY`, so whether RLS applies here depends on whether
    the connecting role owns the tables — which this code cannot know. The two
    branches fail in opposite directions: as `svc_ingress` the tenant policy
    (`:278`) filters everything and this truthfully updates nothing, while as
    an owner RLS is bypassed entirely and an account-only predicate would be a
    cross-tenant write with no workspace bound at all. Carrying the workspace
    the resolve already returned makes the statement correct under both, and
    costs nothing under either. `command_executors.disconnect_account:437`
    scopes the same way for the same reason.
    """
    if not accounts:
        return 0
    result = await conn.execute(
        text(
            "UPDATE oauth_credentials SET state = 'revoked'"
            " WHERE (ig_account_id, workspace_id) IN ("
            "   SELECT * FROM unnest(CAST(:ids AS uuid[]), CAST(:wss AS uuid[]))"
            " ) AND state <> 'revoked'"
        ),
        {
            "ids": [a.ig_account_id for a in accounts],
            "wss": [a.workspace_id for a in accounts],
        },
    )
    return result.rowcount or 0
