"""The Drive connect leg — authorization, code exchange, and the first
non-`ig_login` credential (P3 of the gdrive credential epic).

A target-tier sibling of :mod:`google_oidc` (epic F2 (b)), not an extension of
it: sign-in and resource authorization share a provider, not a purpose. The
sign-in leg asks for identity scopes and writes no credential; this leg asks
for Drive read access WITH offline consent and writes the credential the read
door (:mod:`drive_credentials`) and the Drive adapter live on. It shares the
state machinery (`ig_login_oauth.issue_state` / `consume_state`, the
`oauth_states` row pinning user and workspace) and the encryption ring; it
owns the URL, the exchange, the payload shape and the INSERT.

## What is taken from the legacy flow, and how

`src/services/integrations/google_drive_oauth.py` is a complete, working Drive
OAuth flow — and it is CITED here, not lifted. It imports `telegram.Bot`,
`TokenRepository`, `ChatSettingsRepository` and `BaseService`, and a port would
drag all four across the tier boundary the target architecture exists to draw
(#982 set the convention for the sibling read leg: reference, not import; the
target tier imports nothing legacy). What transfers is knowledge:

- **The scope** (legacy `:40-59`): `drive.readonly` is the narrowest scope that
  lists AND downloads a pre-existing folder. `drive.file` sees only files the
  app created or opened, and user media predates the app; `drive.metadata.readonly`
  lists but cannot download. The narrower path is the Google Picker under
  `drive.file`, which changes onboarding from "paste a folder id" to a Picker
  widget — tracked as #327. This leg drops the legacy `userinfo.email` scope:
  nothing in the target schema stores the granting account's email.
- **The offline-consent shape** (legacy `:99-108`): `access_type=offline` is
  what makes Google issue a refresh token, and `prompt=consent` is what makes
  it issue one AGAIN on a repeat grant — without it a reconnect returns an
  access token only, and the credential quietly expires an hour later.
- **The exchange shape**: `grant_type=authorization_code` against the token
  endpoint. Through :func:`egress.request`, never raw `httpx` (the floor is the
  argument `google_oidc` rests on), and never `google-auth` (F3a (ii): a
  library doing its own I/O voids the floor's allowlist, byte cap, budget and
  private-address block).

## The credential row, and why its payload is an envelope

`oauth_credentials.encrypted_payload` is ONE column, and F3 (b) needs two
tokens in it: the durable refresh token, from which short-lived access tokens
are minted on demand (P5), and — until P5 lands — the access token issued at
connect time, which the read door hands back as-is. Both ride in one
encrypted, versioned JSON envelope (:func:`encode_payload` /
:func:`decode_payload`); `expires_at` is the ACCESS token's expiry, which the
read door refuses past today and P5 mints past. `next_refresh_at` is NULL by
design: migration `063` fences the refresh clock to `provider = 'ig_login'`,
so a gdrive row with it set would assert a refresh nothing performs. The fence
stays closed (F3 (b)); minting happens on the read path.

The row is source-owned: `media_source_id` set, `ig_account_id` NULL,
`provider = 'gdrive'`. `ck_credentials_one_owner` only counts non-null owner
columns, so an `ig_login` row hung off a Drive source is a shape the database
accepts — the gate asserts all three explicitly, and this module never takes
an account id at all.

:func:`store_credential` runs on the CALLER's connection so the connect flow
can re-arm the source (`state='active'`, `alerted_at=NULL`,
`next_sync_at=now()`) in the same transaction (F4 (a), P4): "a credential now
exists" and "this source is eligible again" become one fact that cannot drift.

Operational note (fleet knowledge `google-drive-token-refresh-2026-07-15`):
while the Google OAuth app is in Testing mode, Google expires refresh tokens
after seven days; publishing the app to Production is the ops step that
removes that reconnect cadence. Neither this leg nor P5 can change it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlencode

from sqlalchemy import text

from src.exceptions.base import StorydumpError
from src.services.target import egress
from src.services.target.egress import EgressPolicy
from src.services.target.google_oidc import AUTHORIZE_URL, TOKEN_URL

__all__ = [
    "AUTHORIZE_URL",
    "TOKEN_URL",
    "PROVIDER",
    "SCOPE",
    "PAYLOAD_VERSION",
    "REASONS",
    "DriveGrant",
    "DriveOAuthRefused",
    "DrivePayloadMalformed",
    "authorization_url",
    "exchange_code",
    "encode_payload",
    "decode_payload",
    "store_credential",
]

#: `ck_credentials_provider` / `ck_sources_provider` / `ck_oauth_state_provider`.
PROVIDER = "gdrive"

#: The one scope this leg asks for — see the module docstring for the two
#: narrower ones that were evaluated and rejected, and #327 for the Picker.
SCOPE = "https://www.googleapis.com/auth/drive.readonly"

#: The envelope's version. A payload without it, or with another, is
#: malformed — never guessed at.
PAYLOAD_VERSION = 1

#: Closed set of refusal reasons; the route maps each to a redirect reason.
REASONS: tuple[str, ...] = (
    "exchange_failed",
    "malformed_response",
    "no_refresh_token",
    "scope_not_granted",
)


class DriveOAuthRefused(StorydumpError):
    """The grant could not be completed, with a reason from :data:`REASONS`.
    The message names the refusal; no token ever rides in it."""

    def __init__(self, reason: str, detail: Optional[str] = None):
        if reason not in REASONS:
            raise ValueError(f"unknown refusal reason {reason!r}")
        self.reason = reason
        super().__init__(detail or reason)


class DrivePayloadMalformed(StorydumpError):
    """A decrypted payload that is not a v1 envelope. Raised on READ, so the
    reader can refuse by name rather than send a JSON blob as a bearer token."""


@dataclass(frozen=True)
class DriveGrant:
    """What the token endpoint issued for an offline `drive.readonly` grant."""

    access_token: str
    refresh_token: str
    expires_at: Optional[datetime]
    scope: str


def authorization_url(*, client_id: str, redirect_uri: str, state: str) -> str:
    """Where the browser is sent. Offline + consent, for the reasons in the
    module docstring; no nonce, because this is not an OIDC flow — the
    `oauth_states` row pins the user and workspace the callback acts for."""
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


async def exchange_code(
    client,
    *,
    code: str,
    redirect_uri: str,
    client_id: str,
    client_secret: str,
    now: Optional[datetime] = None,
) -> DriveGrant:
    """Trade the authorization code for the grant, through the egress floor.

    Refuses by name rather than storing a grant it cannot use: no refresh
    token (F3 (b) rests on one — the URL asked with `prompt=consent`, so its
    absence is a real anomaly, not the first-consent gotcha), or a scope the
    consent screen narrowed below `drive.readonly`. The response body is never
    logged; it carries bearer tokens.
    """
    response = await egress.request(
        client,
        "POST",
        TOKEN_URL,
        policy=EgressPolicy(timeout_class="standard"),
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
    )
    if response.status_code != 200:
        raise DriveOAuthRefused(
            "exchange_failed", f"token endpoint answered {response.status_code}"
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise DriveOAuthRefused(
            "malformed_response", "token endpoint body is not JSON"
        ) from exc
    if not isinstance(body, dict):
        raise DriveOAuthRefused(
            "malformed_response", "token endpoint body is not an object"
        )
    access_token = body.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise DriveOAuthRefused("malformed_response", "no access token in the grant")
    refresh_token = body.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise DriveOAuthRefused(
            "no_refresh_token",
            "Google issued no refresh token; the grant would expire within the"
            " hour and strand the source, so it is refused rather than stored",
        )
    granted = body.get("scope")
    granted_set = set(granted.split()) if isinstance(granted, str) else set()
    if SCOPE not in granted_set:
        raise DriveOAuthRefused(
            "scope_not_granted", "the consent screen did not grant drive.readonly"
        )
    expires_in = body.get("expires_in")
    expires_at = None
    if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool):
        expires_at = (now or datetime.now(timezone.utc)) + timedelta(
            seconds=int(expires_in)
        )
    return DriveGrant(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
        scope=" ".join(sorted(granted_set)),
    )


def encode_payload(grant: DriveGrant) -> str:
    """The v1 envelope, as the plaintext the ring encrypts."""
    return json.dumps(
        {
            "v": PAYLOAD_VERSION,
            "access_token": grant.access_token,
            "refresh_token": grant.refresh_token,
            "scope": grant.scope,
        },
        separators=(",", ":"),
    )


def decode_payload(plaintext: str) -> dict[str, Any]:
    """The envelope's fields, or :class:`DrivePayloadMalformed`. A bare token
    string — the shape the read door once assumed nothing would write — is
    malformed too: there is no reader-side guess about what it might be."""
    try:
        envelope = json.loads(plaintext)
    except ValueError as exc:
        raise DrivePayloadMalformed("payload is not JSON") from exc
    if not isinstance(envelope, dict) or envelope.get("v") != PAYLOAD_VERSION:
        raise DrivePayloadMalformed("payload is not a v1 envelope")
    for key in ("access_token", "refresh_token"):
        value = envelope.get(key)
        if not isinstance(value, str) or not value:
            raise DrivePayloadMalformed(f"payload has no {key}")
    return envelope


def _ring():
    """The shipped ring — `07` §3 keeps the env name `ENCRYPTION_KEYS`."""
    from src.utils.encryption import TokenEncryption

    return TokenEncryption()


async def store_credential(
    conn, *, workspace_id, media_source_id, grant: DriveGrant
) -> str:
    """Write the source's `gdrive` credential — or replace the one it already
    has, in place, on a reconnect (`uq_credential_per_source` admits one row
    per source and provider; same id, no gap, no second row). Returns the id.

    On the caller's connection, inside the caller's transaction: the tenant
    and actor GUCs are the unit of work's, `p_tenant` binds the row to the
    workspace, and P4's re-arm of the source lands beside this write.
    """
    result = await conn.execute(
        text(
            "INSERT INTO oauth_credentials"
            " (workspace_id, media_source_id, provider, encrypted_payload,"
            "  expires_at, next_refresh_at, state)"
            # next_refresh_at NULL: the 063 fence keeps gdrive out of the
            # refresh clock (F3 (b)); minting happens on the read path (P5).
            " VALUES (:ws, :src, :provider, :payload, :exp, NULL, 'active')"
            " ON CONFLICT (workspace_id, media_source_id, provider)"
            "   WHERE media_source_id IS NOT NULL"
            " DO UPDATE SET encrypted_payload = EXCLUDED.encrypted_payload,"
            "               expires_at = EXCLUDED.expires_at,"
            "               next_refresh_at = NULL,"
            "               state = 'active'"
            " RETURNING id"
        ),
        {
            "ws": str(workspace_id),
            "src": str(media_source_id),
            "provider": PROVIDER,
            "payload": _ring().encrypt(encode_payload(grant)),
            "exp": grant.expires_at,
        },
    )
    return str(result.scalar_one())
