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
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from secrets import token_hex
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import text

from src.services.target import readers
from src.utils.logger import logger

if TYPE_CHECKING:
    from src.services.target.instagram_catalog import TombstoneKeys

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
    return [value for _, value in _candidates()]


def _candidates() -> list[tuple[str, str]]:
    """The settings a callback may be signed with, preferred first, legacy
    second — the ONE spelling of the order `app_secrets()` and
    `app_secret_names()` share. Each is read off `settings` by name, so the
    reader pin (`test_legacy_settings_gone`) still sees the field is read."""
    from src.config.settings import settings

    pairs = (
        ("INSTAGRAM_APP_SECRET", settings.INSTAGRAM_APP_SECRET),
        ("FACEBOOK_APP_SECRET", settings.FACEBOOK_APP_SECRET),
    )
    return [(name, value) for name, value in pairs if value]


def app_secret_names() -> list[str]:
    """The setting names behind :func:`app_secrets`, in the same order.

    For the one line the route logs after a callback verifies: WHICH setting
    signed it, by name and position, never by value. Which Meta app the
    callback URLs are registered under is a dashboard fact this code cannot
    read; one press of Meta's test button and this line answer it. A
    callback verifies against `INSTAGRAM_APP_SECRET` first, then
    `FACEBOOK_APP_SECRET`, each only when it is set.
    """
    return [name for name, _ in _candidates()]


#: How far a verified payload's `issued_at` may sit from a grant's time and still
#: order the two: closer than this, the order is ambiguous. The
#: database's clock judges it, inside the deletion door and the deauthorize
#: classification, both of which take this value.
ORDERING_SKEW_SECONDS = 300


#: What `app_identity` answers while the Instagram app id is unset or is not an
#: app id. It names no app, so no mapping is ever recorded under it.
UNCONFIGURED_APP = "instagram:unconfigured"


def app_identity(setting_name: str) -> str:
    """The Meta app a callback was signed by, named by the setting that verified it.

    Stable across a secret's rotation: the setting names the app, never the
    value. The Instagram app is `instagram:<INSTAGRAM_APP_ID>`, or
    `instagram:unconfigured` while the id is unset or not an app id; the legacy
    Facebook secret is `facebook:legacy`. Only `instagram:<digits>` identities
    are ever mapped to an account (`meta_identity_mappings`), so the two apps cannot alias on a raw
    subject.
    """
    from src.config.settings import settings

    if setting_name == "INSTAGRAM_APP_SECRET":
        app_id = (settings.INSTAGRAM_APP_ID or "").strip()
        if re.fullmatch("[0-9]{1,32}", app_id):
            return f"instagram:{app_id}"
        return UNCONFIGURED_APP
    return "facebook:legacy"


@dataclass(frozen=True)
class VerifiedRequest:
    """A `signed_request` that verified, and what its verification established.

    `setting_name` names the setting whose secret verified it, and so the Meta
    app that signed it (`app_identity`). When two settings hold that one secret
    no setting can say which app signed, so it names the legacy one: the app
    that is never mapped to an account. `encoded_payload` is the signed
    segment exactly as received: the one thing a replay of the same request is
    recognised by. The payload, the secret and the segment stay out of `repr`,
    so the object cannot put a subject or a secret into a log line.
    """

    payload: dict[str, Any] = field(repr=False)
    secret: str = field(repr=False)
    setting_name: str
    encoded_payload: str = field(repr=False)


def verify_request(signed_request: Optional[str]) -> VerifiedRequest:
    """`verify_signed_request` against this deployment's own candidates, with
    the app that signed it named.

    The name and the secret come from the one `_candidates()` pairing, so they
    cannot disagree.

    **Two settings holding the same secret cannot tell the apps apart.** The
    signature verifies against both, and which app signed is exactly what the
    secret was to say: naming the preferred one would read a request the legacy
    Facebook app signed as Instagram's, and map its subject onto an Instagram
    account. Such a request is ambiguous, so it is named for the legacy setting
    (`app_identity` `facebook:legacy`): no mapping answers to it, and a deletion
    request is held for review (`app_unrecognized`). A warning says so, once
    for the request, because the fix is the configuration's: give each app its
    own secret.
    """
    candidates = _candidates()
    payload, secret = verify_signed_request(
        signed_request, [value for _, value in candidates]
    )
    names = [name for name, value in candidates if value == secret]
    if len(names) > 1:
        logger.warning(
            "meta callback: %s hold the same app secret, so the app that signed"
            " this request cannot be told; it is treated as the legacy app",
            " and ".join(names),
        )
        name = "FACEBOOK_APP_SECRET"
    else:
        name = names[0]
    # Verified, so a string of exactly two segments: signature, then payload.
    return VerifiedRequest(payload, secret, name, signed_request.split(".")[1])


#: Domain separation for a deletion request's fingerprint: an HMAC over this
#: prefix, the app identity and the signed payload segment, so the same bytes
#: under another purpose or another app are another value.
_FINGERPRINT_DOMAIN = b"storydump:meta-deletion-request:v1\x00"
#: The receipt door's bound on the fingerprints one request may present.
MAX_FINGERPRINTS = 16


def request_fingerprints(
    keys: "TombstoneKeys", app_identity: str, encoded_payload: str
) -> list[tuple[int, str]]:
    """One `(key version, hex)` per key in the ring, the current version first.

    The receipt door inserts the first and matches a replay against any, so a
    rotation never splits one request into two episodes. Nothing of the
    payload, the subject or the secret is kept: a fingerprint is a keyed
    digest of the signed segment. A ring past the door's bound is cut to its
    newest versions after the current one; a request first seen under a
    version that old is then a new episode rather than a replay.
    """
    message = (
        _FINGERPRINT_DOMAIN
        + app_identity.encode("ascii")
        + b"\x00"
        + encoded_payload.encode("ascii")
    )
    older = sorted((v for v in keys.keys if v != keys.current_version), reverse=True)
    versions = [keys.current_version, *older][:MAX_FINGERPRINTS]
    return [
        (version, hmac.new(keys.keys[version], message, hashlib.sha256).hexdigest())
        for version in versions
    ]


def order_evidence(issued_at: Any) -> Optional[datetime]:
    """A verified payload's `issued_at` as an instant, when it is an integer
    timestamp. Python only type-checks it: the database's clock judges whether
    it is believable (`ORDERING_SKEW_SECONDS`)."""
    if type(issued_at) is not int:
        return None
    try:
        return datetime.fromtimestamp(issued_at, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def verify_signed_request(
    signed_request: Optional[str], secrets: list[str]
) -> tuple[dict[str, Any], str]:
    """Verify against each candidate secret, in order; the first match wins.

    **Nothing in the payload is decoded or parsed until the signature has
    verified.** Once it has, a refusal is final.

    **The signature covers the RAW base64 payload string, not the decoded
    JSON.** Re-encoding the parsed object and signing that would produce a
    different byte string for the same logical payload (key order, separators),
    so the check would fail for honest requests and — worse — a lenient
    implementation that re-serialised could be steered by whitespace. The
    received bytes are what get verified.

    Returns the payload **and the secret that verified it**, so a caller
    needing that secret afterwards (to derive a receipt code) takes it from the
    verification that just succeeded rather than re-reaching into settings. It
    is then impossible to write the `or ""` fallback that would mint a
    receipt keyed on an empty HMAC key — attacker-computable, and guarded only
    by a comment. The invariant is carried by the return type instead.

    An empty `secrets` refuses, exactly as a single absent secret does — the
    fail-closed direction is a property of having nothing to verify with, not
    of how many settings were consulted. An empty value is not a secret, so it
    is never a candidate.
    """
    candidates = [secret for secret in secrets if secret]
    if not candidates:
        logger.warning("meta callback: refused, no Meta app secret configured")
        raise SignedRequestInvalid("not configured")
    if not signed_request:
        raise SignedRequestInvalid("absent")

    parts = signed_request.split(".")
    if len(parts) != 2:
        raise SignedRequestInvalid("malformed")
    encoded_sig, encoded_payload = parts

    try:
        received_sig = _b64url_decode(encoded_sig)
        # base64url is ASCII, so a segment outside it is undecodable.
        signed_bytes = encoded_payload.encode("ascii")
    except ValueError:
        raise SignedRequestInvalid("undecodable")

    for secret in candidates:
        expected_sig = hmac.new(
            secret.encode("utf-8"), signed_bytes, hashlib.sha256
        ).digest()
        if hmac.compare_digest(received_sig, expected_sig):
            return _decode_payload(encoded_payload), secret
    raise SignedRequestInvalid("signature mismatch")


def _decode_payload(encoded_payload: str) -> dict[str, Any]:
    """Decode and check the payload of a `signed_request` whose signature has
    verified. Called only after a match, never before."""
    try:
        payload_bytes = _b64url_decode(encoded_payload)
    except ValueError:
        raise SignedRequestInvalid("undecodable")

    try:
        payload = json.loads(payload_bytes)
    except (ValueError, RecursionError):
        # Nesting past the interpreter's recursion limit is refused like any other
        # payload that is not JSON: RecursionError is not a ValueError.
        raise SignedRequestInvalid("payload not json")
    if not isinstance(payload, dict):
        raise SignedRequestInvalid("payload not an object")

    # Checked against a constant. A payload naming any other algorithm is
    # rejected outright rather than dispatched on.
    if payload.get("algorithm") != _EXPECTED_ALGORITHM:
        raise SignedRequestInvalid("unexpected algorithm")
    return payload


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

    **This is the receipt of a deployment without the archive's key ring**:
    logged, never stored. With the ring the receipt is a durable row
    (`receive_deletion`) and its code a random capability, not a derivation.
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

    Second: **`ig_accounts` is tenant-scoped under RLS** (`07_row_security.sql`, `p_tenant`
    for `svc_ingress`/`svc_worker`), and a Meta callback names no workspace,
    so there is no tenant to set: a SELECT at the table returns zero rows
    under the runtime login regardless of what is stored, and the route
    reports a miss as "not established" rather than "none". It reads
    through `fn_meta_accounts_for_ref`, a SECURITY DEFINER door
    owned by `svc_maintenance` (fleet health functions): an equality on one bound value,
    every workspace's accounts, EXECUTE for `svc_ingress`. The first reason
    stands, so an empty answer is still not proof of absence.
    """
    rows = (
        await conn.execute(
            text(
                "SELECT o_ig_account_id, o_workspace_id"
                " FROM fn_meta_accounts_for_ref(:ref)"
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

    **One tenant-scoped UPDATE per workspace, under that workspace's tenant
    and a named actor.** `oauth_credentials` is policy-covered, and a write is
    not given a door: the resolve returned each account's workspace, so this
    claims that tenant (`app.tenant_id`, transaction-local — the GUC every
    unit of work sets) and updates the pair under the policy, exactly as a
    member's own disconnect does (`command_executors.disconnect_account`).
    Under the owner login the tenant claim is inert and the pair predicate
    still bounds the write to the workspace; under `svc_ingress` it is what
    makes the UPDATE reach its rows at all — without it the policy filters
    everything and this truthfully updates nothing (fleet health functions). The actor is
    `system`, as the worker's own writes claim: `trg_governance_audit`
    refuses an anonymous mutation of this table under EVERY login, so a
    matched deauthorize without an actor would raise, and Meta would retry
    into the same raise.
    """
    if not accounts:
        return 0
    by_workspace: dict[str, list[str]] = {}
    for a in accounts:
        by_workspace.setdefault(a.workspace_id, []).append(a.ig_account_id)
    revoked = 0
    for workspace_id, ids in by_workspace.items():
        await conn.execute(
            text(
                "SELECT set_config('app.tenant_id', :ws, true),"
                "       set_config('app.actor_kind', 'system', true)"
            ),
            {"ws": workspace_id},
        )
        result = await conn.execute(
            text(
                "UPDATE oauth_credentials SET state = 'revoked'"
                " WHERE workspace_id = CAST(:ws AS uuid)"
                "   AND ig_account_id = ANY(CAST(:ids AS uuid[]))"
                "   AND state <> 'revoked'"
            ),
            {"ws": workspace_id, "ids": ids},
        )
        revoked += result.rowcount or 0
    return revoked


#: The two shapes of confirmation code. A durable receipt's is a random 128-bit
#: capability, hex; the 16 hex characters before it are the logged-only receipt
#: a deployment without `ARCHIVE_HMAC_KEYS` still hands out (`confirmation_code`).
CAPABILITY_CODE = re.compile("[0-9a-f]{32}")
LEGACY_CODE = re.compile("[0-9a-f]{16}")

#: What a deletion request's subject must look like for the receipt door: Meta's
#: app-scoped ids are digits, and the door refuses anything else.
RECEIPT_SUBJECT = re.compile("[0-9]{1,64}")


@dataclass(frozen=True)
class DeletionReceipt:
    """What the receipt door answered one delivery. `code` is this delivery's
    own capability, whose digest is all the database keeps; a replay is the
    same receipt behind a new code."""

    receipt_id: str
    replay: bool
    state: str
    code: str = field(repr=False)


def _digest(code: str) -> str:
    return hashlib.sha256(code.encode("ascii")).hexdigest()


async def receive_deletion(
    engine,
    *,
    app_identity: str,
    fingerprints: list[tuple[int, str]],
    subject: str,
    issued_at: Any,
) -> DeletionReceipt:
    """Record a verified deletion request through the tenant-free door (Meta deletion receipt functions) and
    mint the capability that reads its status back.

    One transaction, as the API's own login with no tenant: the door classifies
    every mapping of (app identity, subject) under each workspace's source lock,
    freezes the scope, and stores the request's keyed fingerprint, never its
    payload or subject. `fingerprints` leads with the current key version's
    (`request_fingerprints`). The order evidence is typed here and judged by the
    database's clock, inside the door.
    """
    code = token_hex(16)
    async with engine.begin() as conn:
        row = await readers.row(
            conn,
            "SELECT o_receipt_id::text AS receipt, o_replay AS replay, o_state AS state"
            " FROM fn_meta_deletion_receive(CAST(:app AS text),"
            " CAST(:fingerprints AS text[]), CAST(:version AS int), CAST(:subject AS text),"
            " CAST(:issued AS timestamptz), CAST(:capability AS text), CAST(:skew AS int))",
            app=app_identity,
            fingerprints=[digest for _, digest in fingerprints],
            version=fingerprints[0][0],
            subject=subject,
            issued=order_evidence(issued_at),
            capability=_digest(code),
            skew=ORDERING_SKEW_SECONDS,
        )
    if row is None:  # pragma: no cover — the door always answers a row
        raise RuntimeError("the receipt door answered no row")
    return DeletionReceipt(row["receipt"], row["replay"], row["state"], code)


async def deletion_status(engine, code: str) -> Optional[str]:
    """`in_progress` or `completed` for the receipt this capability names, or
    None when none does. The door is tenant-free and reveals nothing else."""
    async with engine.begin() as conn:
        row = await readers.row(
            conn,
            "SELECT o_state AS state FROM fn_meta_deletion_status(CAST(:hash AS text))",
            hash=_digest(code),
        )
    return None if row is None else row["state"]
