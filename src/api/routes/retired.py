"""Retired surfaces that live users can still reach — and where they land now.

`/webapp/onboarding` was the Telegram Mini App. This PR deletes it, and the
deployment fact that seemed to make that free — the legacy bot does not run
under `WORKER_IMPL=target`, so it cannot SEND a new Mini App button — does
not cover the buttons it already sent. `build_webapp_button`
(`src/services/core/telegram_utils.py`) bakes a static URL into the message
at send time (a `WebAppInfo` in private chats, a plain URL button in groups),
and tapping either is client-side navigation that never contacts the bot.
navi measured the population: `legacy.user_interactions` was written until
`2026-08-24 01:16:10`, about an hour before the cutover, by exactly the
commands that mint this button (`/start`, `/setup`, `/settings`, add-account).
Real people hold a live button that would otherwise open a 404.

So the old path answers with a redirect to the one surface that now exists
for a person: the web front end's sign-in. The legacy `?chat_id=` is dropped
on purpose — it is a legacy tenant key, and nothing on the new surface may be
handed one. With no front-end origin configured the honest answer is **410
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
