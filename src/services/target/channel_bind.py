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

Refusals before the tapper is proven to be the minting admin are silent — the
router's existence-oracle rule; after that they are answered, and a bound group
is told so, since the door acknowledges handled starts (#1239).
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from src.services.target import (
    bindings,
    identity,
    ig_login_oauth,
    readers,
    unit_of_work,
)
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
    """Consume a `bind-` payload and bind the chat it arrived in.

    Two gates before anything is written, both silent on refusal (the
    router's existence-oracle rule): the state must be live, and the tapper
    must BE the admin who minted it — the link is not a bearer of the
    workspace's card stream (#1240 review). The tapper is known by their
    linked Telegram identity, so an admin links (clause 1) before they bind;
    the mint route refuses up front when they have not.

    Once the tapper is proven to be the minting admin, refusals may speak:
    a link opened in a DM and a group another workspace holds are answered,
    because the person reading the answer is the one entitled to it, and a
    silent spent link is a trap.

    The write runs on the door's raw connection, which carries no tenant or
    actor context of its own; the consumed state row — minted at the admin
    floor, CAS-consumed — IS this lane's pre-context path to a workspace, so
    its ids become the transaction-local GUCs the governance audit trigger
    and the tenant policies require (`unit_of_work.apply_gucs`, the one
    spelling). They die at the route's commit.
    """
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
    workspace_id, minter = row["workspace_id"], row["user_id"]
    if workspace_id is None or minter is None:
        logger.error("group bind: bind state without workspace/user — CHECK missing?")
        return StartResult(outcome="state_without_workspace", handled=False)
    tapper = await identity.user_for_identity(
        conn, provider=PROVIDER, external_id=ctx.telegram_user_id
    )
    if tapper != str(minter):
        logger.warning("group bind: the tapper is not the admin who minted the link")
        return StartResult(outcome="tapper_not_minter", handled=False)
    await unit_of_work.apply_gucs(
        conn,
        tenant_id=str(workspace_id),
        actor_kind="user",
        actor_user_id=str(minter),
        channel="telegram",
    )
    if ctx.chat_type not in GROUP_CHAT_TYPES:
        # Spent either way — a link opened in a DM must not stay usable for a
        # group later. The admin is told, since it is them reading it.
        logger.warning("group bind: opened in a %s chat, not a group", ctx.chat_type)
        return StartResult(
            outcome="not_a_group",
            handled=True,
            reply=(
                "That link only works from Telegram's group picker — it has now"
                " been used up. Get a new one from Settings and choose a group."
            ),
        )
    try:
        outcome = await bindings.bind(
            conn,
            workspace_id=str(workspace_id),
            chat_type=ctx.chat_type,
            external_ref=ctx.chat_id,
        )
    except bindings.BindingRefused as exc:
        logger.warning("group bind refused by the writer: %s", exc.reason)
        return StartResult(outcome=exc.reason, handled=False)
    if outcome == bindings.TAKEN:
        logger.warning("group bind: chat %s is held by another workspace", ctx.chat_id)
        return StartResult(
            outcome="taken",
            handled=True,
            reply=(
                "This group already belongs to another Storydump workspace, so"
                " nothing changed. A group can be bound to one workspace only."
            ),
        )
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
