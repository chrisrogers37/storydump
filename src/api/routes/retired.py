"""Retired surfaces that live users can still reach — and where they land now.

`/webapp/onboarding` is the Mini App URL that buttons already sent carry
(#1216): a static URL baked into the message at send time (a `WebAppInfo` in
private chats, a plain URL button in groups), and tapping either is
client-side navigation that never contacts the bot. Real people hold a live
button that would otherwise open a 404.

So the path answers with a redirect to the one surface for a person: the web
front end's sign-in. The legacy `?chat_id=` is dropped on purpose — it is a
legacy tenant key, and nothing on the web surface may be handed one. With no
front-end origin configured the honest answer is **410
Gone** with a sentence, not a 404 that looks like a broken deployment.

302 rather than 301: the target is configuration (`WEB_APP_URL`), and a
permanent redirect cached inside a Telegram webview would pin whatever the
origin was the day it was tapped.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse, RedirectResponse, Response

from src.config.settings import settings

router = APIRouter(tags=["retired"])

#: Where a retired Mini App link lands on the front end.
LANDING_PATH = "/login"

RETIRED_MESSAGE = "The Telegram Mini App has been retired. Sign in on the web to manage your workspace."


@router.get("/webapp/onboarding", include_in_schema=False)
async def retired_mini_app() -> Response:
    origin = settings.web_app_origin
    if origin:
        return RedirectResponse(f"{origin}{LANDING_PATH}", status_code=302)
    return PlainTextResponse(RETIRED_MESSAGE, status_code=410)
