"""The one settings read every OAuth leaf shares: turn a provider's
(id, secret) settings and a registered callback path into
``(client_id, client_secret, redirect_uri)``, or refuse with a 503 that names
what is missing. A leg that is not configured never half-works.

`google_client` and `instagram_client` are the leaves; each names its own
settings and callback path and delegates here, so the refusal shape and the
redirect-URI join exist once.
"""

from __future__ import annotations

from fastapi import HTTPException

from src.config.settings import settings

BASE_SETTING = "OAUTH_REDIRECT_BASE_URL"


def configured(
    *, label: str, id_setting: str, secret_setting: str, callback_path: str
) -> tuple[str, str, str]:
    missing = [
        name
        for name in (id_setting, secret_setting, BASE_SETTING)
        if not getattr(settings, name, None)
    ]
    if missing:
        raise HTTPException(
            status_code=503,
            detail=f"{label} oauth not configured: set {', '.join(missing)}",
        )
    base = getattr(settings, BASE_SETTING).rstrip("/")
    return (
        getattr(settings, id_setting),
        getattr(settings, secret_setting),
        f"{base}{callback_path}",
    )
