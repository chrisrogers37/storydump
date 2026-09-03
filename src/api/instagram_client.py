"""The Instagram Login OAuth client — its one registered callback path, and the
settings read that turns it into ``(app_id, app_secret, redirect_uri)``.

A leaf, like `google_client`: the connect route and the callback both import
it and it imports neither, so the two cannot disagree on the redirect URI.

The callback path is the LEGACY flow's, byte for byte
(`src/services/integrations/instagram_login_oauth.py`): it is the URI already
registered on the Meta app, so the target tier needs no console change to
take the flow over (#1220).
"""

from __future__ import annotations

from fastapi import HTTPException

from src.config.settings import settings

CONNECT_CALLBACK_PATH = "/auth/instagram-login/callback"


def configured() -> tuple[str, str, str]:
    """(app_id, app_secret, redirect_uri), or a 503 that names what is
    missing. A leg that is not configured refuses; it never half-works."""
    missing = [
        name
        for name in (
            "INSTAGRAM_APP_ID",
            "INSTAGRAM_APP_SECRET",
            "OAUTH_REDIRECT_BASE_URL",
        )
        if not getattr(settings, name, None)
    ]
    if missing:
        raise HTTPException(
            status_code=503,
            detail=f"instagram oauth not configured: set {', '.join(missing)}",
        )
    base = settings.OAUTH_REDIRECT_BASE_URL.rstrip("/")
    return (
        settings.INSTAGRAM_APP_ID,
        settings.INSTAGRAM_APP_SECRET,
        f"{base}{CONNECT_CALLBACK_PATH}",
    )
