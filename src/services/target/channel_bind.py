"""Telegram group binding — the `bind-` lane of the `/start` door (`07` §13).

Owner ruling 2026-09-05 (#1175 D-3 token, D-4 same flow for the Nth group):

1. An admin asks Settings for a group link. `issue_bind_state` mints an
   ``oauth_states`` row with ``purpose='bind', provider='telegram'`` pinning
   the admin AND the workspace, and renders ``t.me/<bot>?startgroup=bind-<state>``.
2. Whoever opens it picks a group. Telegram adds the bot there and sends
   ``/start bind-<state>`` in the group; `handle_bind` consumes the state
   one-shot and binds THAT chat to the pinned workspace through the one
   bindings writer, so `uq_binding_external` (a chat binds once) and D13
   (`0..n` per workspace) hold by construction.

Refusals are silent — the router's existence-oracle rule — and a bound group
is told so, since the door acknowledges handled starts (#1239).
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from src.services.target import bindings, ig_login_oauth, readers
from src.services.target.start_router import StartContext, StartResult

logger = logging.getLogger(__name__)

PREFIX = "bind-"
PROVIDER = "telegram"
PURPOSE = "bind"
GROUP_CHAT_TYPES = ("group", "supergroup")


def deep_link(bot_username: str, state: str) -> str:
    """The tap target: `startgroup` opens Telegram's group picker and sends the
    payload as `/start` IN the chosen group — which is the whole trick."""
    return f"https://t.me/{bot_username}?startgroup={PREFIX}{state}"


async def issue_bind_state(
    conn, *, user_id: str, workspace_id: str, bot_username: str
) -> str:
    """Mint a bind state pinning *user_id* and *workspace_id*; return the link.

    Admin floor is the calling route's. One live link per workspace: an
    earlier copy pasted somewhere must not stay usable after a new one is
    minted, so issuing retires the workspace's other live bind states.
    """
    await conn.execute(
        text(
            "UPDATE oauth_states SET consumed_at = now()"
            " WHERE purpose = :purpose AND provider = :provider"
            "   AND workspace_id = :ws AND consumed_at IS NULL"
        ),
        {"purpose": PURPOSE, "provider": PROVIDER, "ws": str(workspace_id)},
    )
    state = await ig_login_oauth.issue_state(
        conn,
        purpose=PURPOSE,
        provider=PROVIDER,
        user_id=user_id,
        workspace_id=workspace_id,
    )
    return deep_link(bot_username, state)


async def handle_bind(conn, ctx: StartContext) -> StartResult:
    """Consume a `bind-` payload and bind the chat it arrived in."""
    try:
        row = await ig_login_oauth.consume_state(
            conn,
            state=ctx.payload,
            expected_purpose=PURPOSE,
            expected_provider=PROVIDER,
        )
    except ig_login_oauth.OAuthStateRefused as exc:
        logger.warning("group bind refused: %s", exc)
        return StartResult(outcome="state_refused", handled=False)
    workspace_id = row["workspace_id"]
    if workspace_id is None:
        logger.error("group bind: bind state with NULL workspace_id — CHECK missing?")
        return StartResult(outcome="state_without_workspace", handled=False)
    if ctx.chat_type not in GROUP_CHAT_TYPES:
        # The state is spent either way: a link opened in a DM must not stay
        # usable for a group later — mint another from Settings.
        logger.warning("group bind: opened in a %s chat, not a group", ctx.chat_type)
        return StartResult(outcome="not_a_group", handled=False)
    outcome = await bindings.bind(
        conn,
        workspace_id=str(workspace_id),
        chat_type=ctx.chat_type,
        external_ref=ctx.chat_id,
    )
    if outcome == bindings.TAKEN:
        logger.warning("group bind: chat %s is held by another workspace", ctx.chat_id)
        return StartResult(outcome="taken", handled=False)
    named = await readers.row(
        conn, "SELECT name FROM workspaces WHERE id = :ws", ws=str(workspace_id)
    )
    name = (named or {}).get("name") or "your workspace"
    return StartResult(
        outcome=outcome,
        handled=True,
        reply=f'This group now receives Storydump\'s approval cards and notices for "{name}".',
    )


def register(router) -> None:
    """Wire this lane into the shared door, beside `link-` and `inv-`."""
    router.register(PREFIX, handle_bind)
