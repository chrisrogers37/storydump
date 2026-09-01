"""Telegram identity linking — the `link-` half of the `/start` door (`07` §2).

Clause 1's *"one Telegram identity managing both workspaces"* half. The flow:

1. An authenticated user asks to link. `issue_link_state` mints an
   ``oauth_states`` row with ``purpose='link', provider='telegram'`` pinning
   that user, and renders ``t.me/<bot>?start=link-<state>``.
2. The user taps it. `handle_link` consumes the state one-shot and attaches
   the tapping Telegram account to the pinned user.

**The state value IS the start token.** `07` §2: *"the state value is the
one-shot start token (unguessable, stored, CAS-consumed; a stateless signed
token could not be one-shot)."* There is no second secret to mint or leak.

## What this deliberately does not touch

Identity is **user-plane**. `07` §2: *"link pins the user but no workspace —
identity is user-plane, not tenant-plane"*, and `ck_oauth_state_context`
enforces ``workspace_id IS NULL`` for this purpose. So linking never names a
workspace and cannot bind a chat to one — `channel_bindings` is a different
plane with its own cap (``uq_binding_external``: one chat, once) and belongs to
the bindings writer. One Telegram account resolves to one user, who may be a
member of many workspaces; a chat still binds to exactly one.
"""

from __future__ import annotations

import logging

from src.services.target import identity, ig_login_oauth
from src.services.target.start_router import StartContext, StartResult

logger = logging.getLogger(__name__)

PREFIX = "link-"
PROVIDER = identity.PROVIDER_TELEGRAM
PURPOSE = "link"


def deep_link(bot_username: str, state: str) -> str:
    """The tap target. The prefix is what keeps this disjoint from `inv-`."""
    return f"https://t.me/{bot_username}?start={PREFIX}{state}"


async def issue_link_state(conn, *, user_id: str, bot_username: str) -> str:
    """Mint a link state for *user_id* and return the deep link.

    Issuance requires an authenticated session — `07` §2 — which the calling
    route enforces; this function is handed the already-authenticated user.
    """
    state = await ig_login_oauth.issue_state(
        conn, purpose=PURPOSE, user_id=user_id, provider=PROVIDER
    )
    return deep_link(bot_username, state)


async def handle_link(conn, ctx: StartContext) -> StartResult:
    """Consume a `link-` payload and attach the tapping identity.

    Returns no reply text on any refusal: refusal copy is the router's, so a
    consumed token and an unknown one read identically to a prober (`07` §5).
    Every distinguishing detail rides the named ``outcome`` into the log.
    """
    try:
        row = await ig_login_oauth.consume_state(
            conn,
            state=ctx.payload,
            expected_purpose=PURPOSE,
            expected_provider=PROVIDER,
        )
    except ig_login_oauth.OAuthStateRefused as exc:
        # Unknown, expired, already consumed, or issued for another purpose —
        # four facts, one reply, distinct outcomes in the log.
        logger.warning("identity link refused: %s", exc)
        return StartResult(outcome="state_refused", handled=False)

    user_id = row["user_id"]
    if user_id is None:
        # ck_oauth_state_context makes this unreachable for purpose='link'.
        # Refused rather than trusted: a NULL here would mean the CHECK is gone.
        logger.error("identity link: link state with NULL user_id — CHECK missing?")
        return StartResult(outcome="state_without_user", handled=False)

    try:
        created = await identity.link_identity(
            conn,
            user_id=str(user_id),
            provider=PROVIDER,
            external_id=ctx.telegram_user_id,
            display_name=ctx.display_name,
        )
    except identity.IdentityAlreadyLinked as exc:
        logger.warning("identity link refused: %s", exc.reason)
        return StartResult(outcome=exc.reason, handled=False)

    return StartResult(
        outcome="linked" if created else "already_linked",
        handled=True,
        reply="Your Telegram account is now linked to Storydump.",
    )


def register(router) -> None:
    """Wire this lane into the shared door."""
    router.register(PREFIX, handle_link)
