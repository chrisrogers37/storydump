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

from src.api import oauth_client

SIGNIN_CALLBACK_PATH = "/auth/google/callback"
DRIVE_CALLBACK_PATH = "/auth/google-drive/callback"


def configured(callback_path: str) -> tuple[str, str, str]:
    """(client_id, client_secret, redirect_uri) for *callback_path*, or a 503
    that names what is missing (`oauth_client.configured`)."""
    return oauth_client.configured(
        label="google",
        id_setting="GOOGLE_CLIENT_ID",
        secret_setting="GOOGLE_CLIENT_SECRET",
        callback_path=callback_path,
    )
