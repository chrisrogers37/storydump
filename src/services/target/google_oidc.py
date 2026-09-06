"""Google sign-in — `07` §1's OIDC authorization-code flow, hosted on the API.

Three legs, one function each, and none of them touches the database or the
request: the authorization URL the browser is sent to, the server-side code
exchange, and verification of the ID token that comes back. The route owns
the state row (`oauth_states` via `ig_login_oauth.issue_state` /
`consume_state`, whose ``signin`` purpose already carries the cookie-nonce
double submit) and the session it mints afterwards.

## The ID token is verified by claims, not by signature — deliberately

The token arrives in the response to OUR request to Google's token endpoint,
over TLS that the egress floor validated against a pinned host. OpenID Connect
Core §3.1.3.7 step 6 covers exactly this case: *"If the ID Token is received
via direct communication between the Client and the Token Endpoint (which it
is in this flow), the TLS server validation MAY be used to validate the
issuer in place of checking the token signature."* A JWKS fetch would add a
second provider call, a key cache with rotation semantics and an RSA
implementation, all to repeat a check the transport already performs. What
MUST still be checked is checked here, and refused by name: ``iss``, ``aud``,
``exp`` (and a future ``iat``), ``nonce``, and the presence of ``sub``. The
plan's D32 rule rides on that last one — identity keys on ``sub``, never on
the email claim, which is metadata here and only trusted when Google marks
it verified.

The nonce binds the token to the browser that started the flow: it is
``SHA256(state)``, so the callback recomputes it from the state it just
consumed rather than storing a second secret.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlencode

from src.exceptions.base import StorydumpError
from src.services.target import egress
from src.services.target.egress import EgressPolicy

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
#: Already in `egress.DEFAULT_ALLOWED_HOSTS` — no allowlist widening needed.
TOKEN_URL = "https://oauth2.googleapis.com/token"
#: Google's revocation endpoint. Same host as the token endpoint, so it is
#: already inside the floor's allowlist — no widening (#1083).
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
#: Google issues under both spellings, and the spec lists both.
ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})
SCOPE = "openid email profile"
#: Tolerated skew on `exp`/`iat`. One minute, the same allowance the legacy
#: URL-token validator used (`webapp_auth.CLOCK_SKEW_TOLERANCE`).
CLOCK_SKEW_SECONDS = 60


class OidcRefused(StorydumpError):
    """A sign-in the flow will not complete. ``reason`` is a closed set so the
    route maps it without parsing prose: ``exchange_failed`` · ``no_id_token``
    · ``malformed_id_token`` · ``issuer`` · ``audience`` · ``expired`` ·
    ``future`` · ``nonce`` · ``subject``."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        super().__init__(
            f"sign-in refused: {reason}" + (f" — {detail}" if detail else "")
        )


@dataclass(frozen=True)
class GoogleIdentity:
    """What a verified ID token says about the person. ``email`` is only
    carried when Google marked it verified; an unverified claim is dropped
    here rather than trusted downstream."""

    sub: str
    email: Optional[str]
    display_name: Optional[str]


def nonce_for(state: str) -> str:
    """The OIDC nonce for a state value — derived, never stored."""
    return hashlib.sha256(state.encode()).hexdigest()


def authorization_url(*, client_id: str, redirect_uri: str, state: str) -> str:
    """Where the browser is sent. ``prompt=select_account`` so a person with
    several Google accounts chooses, instead of being signed in silently as
    whichever one the browser last used."""
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
        "state": state,
        "nonce": nonce_for(state),
        "prompt": "select_account",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


async def code_grant(
    client,
    *,
    code: str,
    redirect_uri: str,
    client_id: str,
    client_secret: str,
) -> tuple[int, Any]:
    """POST the authorization-code grant to the token endpoint, through the
    egress floor, and hand back ``(status, body)`` — the body parsed as JSON,
    or ``None`` when the endpoint did not answer JSON.

    The one request shape both Google legs share: sign-in (below) and the
    Drive connect leg (:mod:`google_drive_oauth`). What a non-200 or a missing
    field MEANS differs per leg, so each caller raises its own refusal; the
    floor's own refusals (host, budget) propagate as themselves. The body is
    never logged — it carries bearer tokens.
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
    try:
        body = response.json()
    except ValueError:
        body = None
    return response.status_code, body


async def refresh_grant(
    client, *, refresh_token: str, client_id: str, client_secret: str
) -> tuple[int, Any]:
    """POST the refresh-token grant to the token endpoint, through the egress
    floor, and hand back ``(status, body)`` exactly as :func:`code_grant` does
    — the Drive read path (P5, F3 (b), #1247) mints its hourly access token
    with this. What a non-200 MEANS is the caller's to say: a 400
    ``invalid_grant`` is a grant Google no longer honours, everything else is
    transient. The body is never logged — it carries a bearer token.
    """
    response = await egress.request(
        client,
        "POST",
        TOKEN_URL,
        policy=EgressPolicy(timeout_class="standard"),
        data={
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
        },
    )
    try:
        body = response.json()
    except ValueError:
        body = None
    return response.status_code, body


async def revoke_token(client, *, token: str) -> int:
    """Ask Google to invalidate a grant. Returns the HTTP status, and raises
    only what the FLOOR raises.

    Through `egress.request` and deliberately not a library: F3a locked
    hand-written Google calls because a library doing its own I/O silently
    voids the allowlist, byte cap, retry budget and private-address block —
    including for :func:`verify_id_token`, whose claims-not-signature argument
    rests on this transport having been floored. Nothing about a revoke needs
    more than one POST.

    The STATUS is handed back rather than a bool, and no refusal is raised,
    because the only caller is best-effort (F5 (a)) and needs to record WHICH
    answer it got. Google returns 200 for a successful revoke and 400 for a
    token that is already invalid — the second is the outcome we wanted, so
    collapsing them into "failed" would event a false alarm every time a user
    disconnects twice. The caller decides; this reports.

    The token is sent in the FORM BODY, never the query string: a URL is the
    thing that reaches proxy logs and error reports.
    """
    response = await egress.request(
        client,
        "POST",
        REVOKE_URL,
        policy=EgressPolicy(timeout_class="standard"),
        data={"token": token},
    )
    return response.status_code


async def exchange_code(
    client,
    *,
    code: str,
    redirect_uri: str,
    client_id: str,
    client_secret: str,
) -> str:
    """Trade the authorization code for the ID token. Returns the raw ID token
    string; nothing else in the token response is used (no refresh token is
    requested — sign-in needs none). Provider-side failures surface as
    ``OidcRefused`` so the route has one refusal type to map.
    """
    status, body = await code_grant(
        client,
        code=code,
        redirect_uri=redirect_uri,
        client_id=client_id,
        client_secret=client_secret,
    )
    if status != 200:
        raise OidcRefused("exchange_failed", f"token endpoint answered {status}")
    if body is None:
        raise OidcRefused("exchange_failed", "token endpoint body is not JSON")
    id_token = body.get("id_token") if isinstance(body, dict) else None
    if not isinstance(id_token, str) or not id_token:
        raise OidcRefused("no_id_token")
    return id_token


def _b64url_decode(segment: str) -> bytes:
    padded = segment + "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(padded.encode())


def decode_id_token(id_token: str) -> dict[str, Any]:
    """The token's claims. Structure only — see the module docstring for why
    the signature is not checked here, and `verify_id_token` for what is."""
    parts = id_token.split(".")
    if len(parts) != 3:
        raise OidcRefused("malformed_id_token", "not three segments")
    try:
        payload = json.loads(_b64url_decode(parts[1]))
    except (ValueError, UnicodeDecodeError) as exc:
        raise OidcRefused("malformed_id_token", "payload is not JSON") from exc
    if not isinstance(payload, dict):
        raise OidcRefused("malformed_id_token", "payload is not an object")
    return payload


def verify_id_token(
    payload: dict[str, Any],
    *,
    client_id: str,
    nonce: str,
    now: Optional[float] = None,
) -> GoogleIdentity:
    """OIDC Core §3.1.3.7's claim checks, each refused by name."""
    now = time.time() if now is None else now

    if payload.get("iss") not in ISSUERS:
        raise OidcRefused("issuer", repr(payload.get("iss")))

    aud = payload.get("aud")
    audiences = aud if isinstance(aud, list) else [aud]
    if client_id not in audiences:
        raise OidcRefused("audience")

    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or now > exp + CLOCK_SKEW_SECONDS:
        raise OidcRefused("expired")
    iat = payload.get("iat")
    if isinstance(iat, (int, float)) and iat > now + CLOCK_SKEW_SECONDS:
        raise OidcRefused("future", "issued-at is ahead of our clock")

    presented = payload.get("nonce")
    if not isinstance(presented, str) or not hmac.compare_digest(presented, nonce):
        raise OidcRefused("nonce")

    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub:
        raise OidcRefused("subject")

    verified = payload.get("email_verified")
    email = payload.get("email")
    email_ok = verified is True or verified == "true"
    name = payload.get("name")
    return GoogleIdentity(
        sub=sub,
        email=email.strip().lower() if (email_ok and isinstance(email, str)) else None,
        display_name=name if isinstance(name, str) and name.strip() else None,
    )
