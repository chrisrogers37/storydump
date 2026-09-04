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
