"""Instagram Login OAuth client config — `google_client`'s sibling.

Named separately from Google's because the two legs answer different
questions and fail differently, and because the app credential here is the
one Meta annotates *preferred* (`INSTAGRAM_APP_SECRET`), not the legacy
Facebook Login pair. Routing the Instagram leg through Google's helper would
have made that distinction invisible at the call site.
"""

from __future__ import annotations

from fastapi import HTTPException

from src.config.settings import settings

#: Where Meta returns the browser. Named once so the route, the redirect_uri
#: sent to Instagram, and the value registered in the app dashboard cannot
#: drift apart — a mismatch there is Meta error 191 and says nothing useful.
CALLBACK_PATH = "/auth/instagram/callback"


def configured(callback_path: str = CALLBACK_PATH) -> tuple[str, str, str]:
    """(client_id, client_secret, redirect_uri), or a 503 naming what is missing.

    A leg that is not configured refuses; it never half-works — `google_client`'s
    rule, kept deliberately identical so the two legs fail the same way.
    """
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
        f"{base}{callback_path}",
    )
