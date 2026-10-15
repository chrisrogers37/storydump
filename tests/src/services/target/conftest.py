"""Helpers the two Google-leg suites share (sign-in and Drive): the one
egress seam they drive the token endpoint through, the grant the Drive tests
build, and the PKCE pair RFC 7636 Appendix B computes. And the invitation
email's recording sender, which its unit tests and its gate both drive.

Plain functions, classes and constants, imported by name — the gates under
`tests/scripts/` read the same builders, and a fixture cannot be imported
across suites.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import httpx

from src.services.target import egress
from src.services.target.google_drive_oauth import DriveGrant


def capture_egress(monkeypatch, *, status=200, body=None, raw=None):
    """Patch `egress.request` — the ONE seam both Google legs post the code
    grant through — to answer *status* with *body* as JSON (*raw* bytes win
    when given), and return the dict the request's shape is recorded into."""
    seen = {}

    async def fake_request(client, method, url, *, policy=None, **kwargs):
        seen.update(method=method, url=url, policy=policy, **kwargs)
        content = raw if raw is not None else json.dumps(body or {}).encode()
        return httpx.Response(
            status, content=content, headers={"content-type": "application/json"}
        )

    monkeypatch.setattr(egress, "request", fake_request)
    return seen


def drive_grant(**over) -> DriveGrant:
    """A grant as the Drive exchange returns it — LIVE against the real clock
    by default. The read door compares `expires_at` with `now()`, so a grant
    pinned to an instant becomes an expired credential the moment the wall
    clock passes that instant."""
    kw = dict(
        access_token="ya29.access",
        refresh_token="1//refresh",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    kw.update(over)
    return DriveGrant(**kw)


class RecordingSender:
    """`email_sender.ResendSender`'s one call, recorded and never sent: each
    send lands in `sent` as ``{to, subject, body, token}``, *token* being the
    link's, an invitation's join link or an admission's claim link, read from
    the body (None without one), then answers a provider ref — or raises
    *raises*."""

    _LINK = re.compile(r"/(?:join|claim)/([A-Za-z0-9_-]+)")

    def __init__(self, *, raises=None):
        self.raises, self.sent = raises, []

    async def send(self, *, to, subject, body):
        link = self._LINK.search(body)
        self.sent.append(
            {
                "to": to,
                "subject": subject,
                "body": body,
                "token": link.group(1) if link else None,
            }
        )
        if self.raises is not None:
            raise self.raises
        return "msg_test"


#: RFC 7636 Appendix B: the example code verifier and its S256 challenge.
APPENDIX_B_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
APPENDIX_B_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
