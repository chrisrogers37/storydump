"""The Instagram Login OAuth client — its one registered callback path, and the
settings read that turns it into ``(app_id, app_secret, redirect_uri)``.

A leaf, like `google_client`: the connect route and the callback both import
it and it imports neither, so the two cannot disagree on the redirect URI.

The callback path is the URI registered on the Meta app (#1216, #1220), byte
for byte.
"""

from __future__ import annotations

from src.api import oauth_client

CONNECT_CALLBACK_PATH = "/auth/instagram-login/callback"


def configured() -> tuple[str, str, str]:
    """(app_id, app_secret, redirect_uri), or a 503 that names what is
    missing (`oauth_client.configured`)."""
    return oauth_client.configured(
        label="instagram",
        id_setting="INSTAGRAM_APP_ID",
        secret_setting="INSTAGRAM_APP_SECRET",
        callback_path=CONNECT_CALLBACK_PATH,
    )
