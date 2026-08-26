"""The one Google OAuth client both legs share — its two registered callback
paths, and the settings read that turns a path into
``(client_id, client_secret, redirect_uri)``.

A leaf: the sign-in router and the Drive connect route both import it and it
imports neither, so the two legs cannot disagree on a redirect URI and the
connect route does not reach into the auth router for its config. ONE Google
client serves both (`GOOGLE_CLIENT_ID`), so both URIs are registered on it;
the Drive one is the legacy flow's path unchanged, byte for byte.
"""

from __future__ import annotations

from fastapi import HTTPException

from src.config.settings import settings

SIGNIN_CALLBACK_PATH = "/auth/google/callback"
DRIVE_CALLBACK_PATH = "/auth/google-drive/callback"


def configured(callback_path: str) -> tuple[str, str, str]:
    """(client_id, client_secret, redirect_uri) for *callback_path*, or a 503
    that names what is missing. A leg that is not configured refuses; it never
    half-works."""
    missing = [
        name
        for name in (
            "GOOGLE_CLIENT_ID",
            "GOOGLE_CLIENT_SECRET",
            "OAUTH_REDIRECT_BASE_URL",
        )
        if not getattr(settings, name, None)
    ]
    if missing:
        raise HTTPException(
            status_code=503,
            detail=f"google oauth not configured: set {', '.join(missing)}",
        )
    base = settings.OAUTH_REDIRECT_BASE_URL.rstrip("/")
    return (
        settings.GOOGLE_CLIENT_ID,
        settings.GOOGLE_CLIENT_SECRET,
        f"{base}{callback_path}",
    )
