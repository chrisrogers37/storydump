"""Helpers the two Google-leg suites share (sign-in and Drive): the one
egress seam they drive the token endpoint through, and the grant the Drive
tests build.

Plain functions, imported by name — the Drive gate under `tests/scripts/`
reads the same grant builder, and a fixture cannot be imported across suites.
"""

from __future__ import annotations

import json
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
    clock passes that instant: that is how this gate went red in CI at
    21:00 UTC on the day it was written."""
    kw = dict(
        access_token="ya29.access",
        refresh_token="1//refresh",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    kw.update(over)
    return DriveGrant(**kw)
