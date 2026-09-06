"""The Drive connect leg — authorization, code exchange, and the first
non-`ig_login` credential (P3 of the gdrive credential epic).

A target-tier sibling of :mod:`google_oidc` (epic F2 (b)), not an extension of
it: sign-in and resource authorization share a provider, not a purpose. The
sign-in leg asks for identity scopes and writes no credential; this leg asks
for Drive read access WITH offline consent and writes the credential the read
door (:mod:`drive_credentials`) and the Drive adapter live on. Shared with the
rest of the tier rather than owned here: the token-endpoint POST
(:func:`google_oidc.code_grant`), the state machinery and the encryption ring
(:mod:`ig_login_oauth`). Owned here: the URL, what a grant must contain to be
kept, the payload shape, and the INSERT.

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
  access token only, and the credential quietly expires an hour later. It is
  also why a grant that comes back WITHOUT a refresh token is refused rather
  than stored (`no_refresh_token`): the URL asked for one, so its absence is
  an anomaly, not the first-consent gotcha.
- **The exchange shape**: `grant_type=authorization_code` against the token
  endpoint, through the egress floor (`google_oidc.code_grant`, the one POST
  both legs share) — never raw `httpx`, and never `google-auth` (F3a (ii): a
  library doing its own I/O voids the floor's allowlist, byte cap, budget and
  private-address block).

## The credential row, and why its payload is an envelope

`oauth_credentials.encrypted_payload` is ONE column, and F3 (b) needs two
tokens in it: the durable refresh token, from which short-lived access tokens
are minted on demand (P5), and — until P5 lands — the access token issued at
connect time, which the read door hands back as-is. Both ride in one
encrypted, versioned JSON envelope (:func:`encode_payload` /
:func:`decode_payload` — the writer's shape and the reader's, one definition);
`expires_at` is the ACCESS token's expiry. `next_refresh_at` is NULL: the
refresh clock is fenced to `ig_login` and minting happens on the read path,
and :mod:`drive_credentials` owns that account (what the `063` fence guards,
and what it does not).

The row is WORKSPACE-owned (069, `07` §15; owner ruling 2026-09-05): `provider =
'gdrive'`, `media_source_id` NULL, `ig_account_id` NULL — `ck_credentials_one_owner`
is provider-conditional and refuses any other shape for `gdrive`, and
`uq_credential_per_workspace` admits one ownerless row per workspace and provider.
The state that leads here is pinned the same way: `oauth_states.reconnect_target`
carries the WORKSPACE id on a `connect` as well as on a `reconnect`, so
`issue_state`'s last-issued-wins retires a stale state per workspace and the
callback refuses a state that names anything else.

Operational note (fleet knowledge `google-drive-token-refresh-2026-07-15`):
while the Google OAuth app is in Testing mode, Google expires refresh tokens
after seven days; publishing the app to Production is the ops step that
removes that reconnect cadence. Neither this leg nor P5 can change it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

from sqlalchemy import text

from src.exceptions.base import RefusalError, StorydumpError
from src.services.target.google_oidc import AUTHORIZE_URL, code_grant, refresh_grant
from src.services.target.ig_login_oauth import ring

#: `ck_credentials_provider` / `ck_sources_provider` / `ck_oauth_state_provider`.
PROVIDER = "gdrive"

#: The one scope this leg asks for — see the module docstring for the two
#: narrower ones that were evaluated and rejected, and #327 for the Picker.
SCOPE = "https://www.googleapis.com/auth/drive.readonly"

#: The envelope's version. A payload without it, or with another, is
#: malformed — never guessed at.
PAYLOAD_VERSION = 1

#: Every reason this leg can refuse a grant for → the error-page reason the
#: callback redirects with. TOTAL over the vocabulary by construction — the
#: refusal's constructor admits nothing outside these keys — so the route can
#: never meet a reason it has no redirect for. Two route reasons: the exchange
#: itself failed, or Google answered with a grant this leg will not keep (no
#: refresh token, or a consent screen that narrowed the scope).
REDIRECT_REASON = {
    "exchange_failed": "exchange_failed",
    "malformed_response": "exchange_failed",
    "no_refresh_token": "grant_incomplete",
    "scope_not_granted": "grant_incomplete",
    # The read-path refresh (P5, #1247) never reaches the callback, but its two
    # refusals live in the one vocabulary so `DriveOAuthRefused` stays total:
    # `grant_revoked` is Google's `invalid_grant` — definitive, the row goes
    # `expired` — and `refresh_failed` is everything transient.
    "grant_revoked": "exchange_failed",
    "refresh_failed": "exchange_failed",
    # `invalid_client` / `unauthorized_client`: OUR client id or secret is
    # wrong — configuration, not the grant and not the weather.
    "client_misconfigured": "exchange_failed",
}


class DriveOAuthRefused(RefusalError):
    """The grant could not be completed. ``reason`` is :data:`REDIRECT_REASON`'s
    key set — exchange_failed | malformed_response | no_refresh_token |
    scope_not_granted | grant_revoked | refresh_failed | client_misconfigured —
    and no token ever rides in the message."""

    _prefix = "drive grant refused"

    def __init__(self, reason: str, detail: str = ""):
        if reason not in REDIRECT_REASON:
            raise ValueError(f"not a drive grant reason: {reason!r}")
        super().__init__(reason, detail)


class DrivePayloadMalformed(StorydumpError):
    """A decrypted payload that is not a v1 envelope. Raised on READ, so the
    reader can refuse by name rather than send a JSON blob as a bearer token."""


@dataclass(frozen=True)
class DriveGrant:
    """What the token endpoint issued for an offline `drive.readonly` grant.
    The granted scope is checked at the exchange and not carried: a grant that
    exists at all is one that carried `drive.readonly`."""

    access_token: str
    refresh_token: str
    expires_at: Optional[datetime]


@dataclass(frozen=True)
class DrivePayload:
    """The envelope, decoded — the two tokens a v1 payload carries. The read
    door hands back `access_token` today; P5 mints from `refresh_token`."""

    access_token: str
    refresh_token: str


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
) -> DriveGrant:
    """Trade the authorization code for the grant, through the egress floor
    (`google_oidc.code_grant`). Refuses by name rather than storing a grant it
    cannot use — no refresh token, or a scope the consent screen narrowed
    below `drive.readonly` (the module docstring has why). `expires_in` is
    relative to the exchange, so the expiry is stamped here.
    """
    status, body = await code_grant(
        client,
        code=code,
        redirect_uri=redirect_uri,
        client_id=client_id,
        client_secret=client_secret,
    )
    if status != 200:
        raise DriveOAuthRefused("exchange_failed", f"token endpoint answered {status}")
    if not isinstance(body, dict):
        raise DriveOAuthRefused(
            "malformed_response", "token endpoint body is not a JSON object"
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
    if SCOPE not in (granted.split() if isinstance(granted, str) else ()):
        raise DriveOAuthRefused(
            "scope_not_granted", "the consent screen did not grant drive.readonly"
        )
    expires_in = body.get("expires_in")
    expires_at = None
    if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool):
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
    return DriveGrant(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
    )


async def refresh_access_token(
    client, *, refresh_token: str, client_id: str, client_secret: str
) -> DriveGrant:
    """Mint a fresh access token from the stored refresh token (P5, F3 (b),
    #1247), through the egress floor (`google_oidc.refresh_grant`).

    Google issues no new refresh token on a refresh as a rule, so the stored
    one rides on in the returned grant; when it does rotate one, the rotated
    token is kept. Refuses by name: ``grant_revoked`` for a 400
    ``invalid_grant`` (the grant is gone on Google's side — revoked by the
    person, or the app removed; D31's definitive class), ``refresh_failed``
    for any other non-200 (transient: this call refuses, the row stands),
    ``malformed_response`` for an answer without a token.
    """
    status, body = await refresh_grant(
        client,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
    )
    if status != 200:
        error = body.get("error") if isinstance(body, dict) else None
        # `invalid_grant` is definitive whatever the status Google chose to
        # put it on (400 today; a 401 has been seen) — the grant is gone.
        if error == "invalid_grant":
            raise DriveOAuthRefused(
                "grant_revoked",
                "the token endpoint no longer honours this refresh token",
            )
        if error in ("invalid_client", "unauthorized_client"):
            raise DriveOAuthRefused(
                "client_misconfigured",
                f"the token endpoint rejected this client ({error}): GOOGLE_CLIENT_ID /"
                " GOOGLE_CLIENT_SECRET are wrong for this project",
            )
        raise DriveOAuthRefused("refresh_failed", f"token endpoint answered {status}")
    if not isinstance(body, dict):
        raise DriveOAuthRefused(
            "malformed_response", "token endpoint body is not a JSON object"
        )
    access_token = body.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise DriveOAuthRefused("malformed_response", "no access token in the refresh")
    rotated = body.get("refresh_token")
    expires_in = body.get("expires_in")
    # No `expires_in` (Google always sends one; a proxy might not): assume
    # Google's hour rather than write NULL, which would read as "no known
    # expiry" and never be refreshed again.
    seconds = 3600
    if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool):
        seconds = int(expires_in)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return DriveGrant(
        access_token=access_token,
        refresh_token=rotated
        if isinstance(rotated, str) and rotated
        else refresh_token,
        expires_at=expires_at,
    )


def encode_payload(grant: DriveGrant) -> str:
    """The v1 envelope, as the plaintext the ring encrypts."""
    return json.dumps(
        {
            "v": PAYLOAD_VERSION,
            "access_token": grant.access_token,
            "refresh_token": grant.refresh_token,
        },
        separators=(",", ":"),
    )


def decode_payload(plaintext: str) -> DrivePayload:
    """The envelope's tokens, or :class:`DrivePayloadMalformed`. A bare token
    string — the shape the read door once assumed nothing would write — is
    malformed too: there is no reader-side guess about what it might be."""
    try:
        envelope = json.loads(plaintext)
    except ValueError as exc:
        raise DrivePayloadMalformed("payload is not JSON") from exc
    if not isinstance(envelope, dict) or envelope.get("v") != PAYLOAD_VERSION:
        raise DrivePayloadMalformed("payload is not a v1 envelope")
    tokens = {}
    for key in ("access_token", "refresh_token"):
        value = envelope.get(key)
        if not isinstance(value, str) or not value:
            raise DrivePayloadMalformed(f"payload has no {key}")
        tokens[key] = value
    return DrivePayload(**tokens)


async def connect_purpose(conn, *, workspace_id) -> str:
    """Which state the connect route mints for this WORKSPACE (069, `07` §15:
    one Google grant per workspace): ``"connect"`` when it holds no `gdrive`
    credential, ``"reconnect"`` once it does, in any state — so `issue_state`'s
    last-issued-wins retires a stale state per workspace."""
    credentialed = (
        await conn.execute(
            text(
                "SELECT EXISTS ("
                "  SELECT 1 FROM oauth_credentials c"
                "   WHERE c.workspace_id = :ws AND c.provider = :provider"
                "     AND c.ig_account_id IS NULL AND c.media_source_id IS NULL"
                ")"
            ),
            {"ws": str(workspace_id), "provider": PROVIDER},
        )
    ).scalar()
    return "reconnect" if credentialed else "connect"


async def store_credential(conn, *, workspace_id, grant: DriveGrant) -> str:
    """Write the WORKSPACE's `gdrive` credential — or replace the one it
    already holds, in place, on a reconnect (`uq_credential_per_workspace`
    admits one ownerless row per workspace and provider; same id, no gap, no
    second row). Returns the id. A `gdrive` credential names no owner column:
    the workspace is its owner (069, #1165).

    On the CALLER's connection, inside the caller's transaction — the F4 (a)
    contract: the tenant and actor GUCs are the unit of work's, `p_tenant`
    binds the row to the workspace, and the re-arm of every `gdrive` source
    (`media_sync.rearm_after_connect`, workspace-wide) lands beside this
    write, so "a grant now exists" and "these folders are eligible again"
    become one fact that cannot drift.
    """
    result = await conn.execute(
        text(
            "INSERT INTO oauth_credentials"
            " (workspace_id, provider, encrypted_payload,"
            "  expires_at, next_refresh_at, state)"
            # next_refresh_at NULL — the read door's header has the fence.
            " VALUES (:ws, :provider, :payload, :exp, NULL, 'active')"
            " ON CONFLICT (workspace_id, provider)"
            "   WHERE ig_account_id IS NULL AND media_source_id IS NULL"
            " DO UPDATE SET encrypted_payload = EXCLUDED.encrypted_payload,"
            "               expires_at = EXCLUDED.expires_at,"
            "               next_refresh_at = NULL,"
            "               state = 'active'"
            " RETURNING id"
        ),
        {
            "ws": str(workspace_id),
            "provider": PROVIDER,
            "payload": ring().encrypt(encode_payload(grant)),
            "exp": grant.expires_at,
        },
    )
    return str(result.scalar_one())
