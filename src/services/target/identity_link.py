"""Telegram identity linking — the `link-` half of the `/start` door (`07` §2).

Clause 1's *"one Telegram identity managing both workspaces"* half. The flow:

1. An authenticated user asks to link. `issue_link_state` mints an
   ``oauth_states`` row with ``purpose='link', provider='telegram'`` pinning
   that user, and renders ``t.me/<bot>?start=link-<state>``.
2. The user opens it. `handle_link` links NOTHING: it peeks the state and
   answers in the private chat with whose Storydump account the link belongs
   to — a masked email — and two buttons, Confirm and Cancel.
3. The same Telegram user taps Confirm. `handle_tap` consumes the state
   one-shot and attaches that Telegram account to the pinned user. Cancel
   consumes it and links nothing; so does the state's expiry, by lapse.

**Why two steps.** A linked Telegram identity speaks for its Storydump
account — its message in a bound group makes that account a member
(`membership_sync`) and its card taps run as that account — so a link
attaches an identity only on the opener's own Confirm. The confirmation names
the account so the opener can see whose it is, and the button carries the
opener's Telegram id, so a Confirm counts only for the user it was offered to.

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
from dataclasses import dataclass
from typing import Optional

from src.services.target import (
    callback_tokens,
    identity,
    oauth_states,
    readers,
    start_router,
)
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

    **One live link per user.** A link is a bearer of the user's identity slot
    (whoever opens it and confirms attaches THEIR Telegram to this user), so an earlier copy
    that was pasted somewhere must not stay tappable after a new one is
    minted: issuing retires the user's other live `link` states in the same
    transaction — the "last issued wins" rule `07` §2 states for reconnects,
    applied to the purpose it matters most for.
    """
    await oauth_states.retire_live_states(
        conn, provider=PROVIDER, purpose=PURPOSE, user_id=user_id
    )
    state = await oauth_states.issue_state(
        conn, purpose=PURPOSE, user_id=user_id, provider=PROVIDER
    )
    return deep_link(bot_username, state)


#: The account a link belongs to, when it has neither an email nor a name.
UNNAMED_ACCOUNT = "an unnamed Storydump account"

CONFIRM_LABEL = "✅ Confirm"
CANCEL_LABEL = "✖️ Cancel"

#: Every refusal a confirmation tap can meet that is about the STATE —
#: unknown, expired, consumed, cancelled, another purpose — reads as this one
#: string, the router's own, so the second step is no more of an oracle than
#: the first.
REFUSAL = start_router.REFUSAL
#: Either half of the pair already holds a Telegram link. One string for both
#: `IdentityAlreadyLinked` reasons: the log names which.
ALREADY_LINKED_ELSEWHERE = (
    "Nothing was linked: this Telegram account, or that Storydump account, is"
    " already linked to another one."
)
CANCELLED = "Cancelled — nothing was linked."


def mask_email(email: Optional[str]) -> Optional[str]:
    """``ada@example.com`` → ``a•••@example.com``; None for anything that is
    not an address. The full address is never rendered: the confirmation is
    read by whoever opened the link, who is exactly the person who must be
    able to tell "that is not my account" without learning whose it is."""
    if not email:
        return None
    local, at, domain = str(email).strip().rpartition("@")
    if not at or not local or not domain:
        return None
    return f"{local[0]}•••@{domain}"


async def account_label(conn, *, user_id: str) -> str:
    """How the confirmation names the Storydump account a link belongs to:
    the masked email, else the account's display name, else
    :data:`UNNAMED_ACCOUNT`. User-plane reads, row-open to the ingress role."""
    found = await readers.row(
        conn,
        "SELECT u.primary_email,"
        "       (SELECT ui.display_name FROM user_identities ui"
        "         WHERE ui.user_id = u.id"
        "           AND ui.display_name IS NOT NULL AND ui.display_name <> ''"
        "         ORDER BY ui.created_at LIMIT 1) AS display_name"
        "  FROM users u WHERE u.id = :u",
        u=str(user_id),
    )
    if found is None:
        return UNNAMED_ACCOUNT
    return (
        mask_email(found.get("primary_email"))
        or found.get("display_name")
        or UNNAMED_ACCOUNT
    )


def confirmation_text(label: str) -> str:
    return (
        f"Link this Telegram account to the Storydump account {label}?\n\n"
        "Only confirm if that account is yours. If someone sent you this link,"
        " tap Cancel — confirming would let them act as you in your groups."
    )


def confirmation_keyboard(state: str, telegram_user_id: str) -> dict:
    """Confirm and Cancel, each carrying the state AND the Telegram user the
    prompt was offered to — minted by the one token module."""
    return {
        "inline_keyboard": [
            [
                {
                    "text": CONFIRM_LABEL,
                    "callback_data": callback_tokens.link_token(
                        callback_tokens.LINK_CONFIRM, state, telegram_user_id
                    ),
                },
                {
                    "text": CANCEL_LABEL,
                    "callback_data": callback_tokens.link_token(
                        callback_tokens.LINK_CANCEL, state, telegram_user_id
                    ),
                },
            ]
        ]
    }


async def handle_link(conn, ctx: StartContext) -> StartResult:
    """Step one of a `link-` payload: ASK, link nothing.

    The opener is shown whose account the link belongs to (a masked email)
    with Confirm and Cancel, and only :func:`handle_tap`'s Confirm, by the
    same Telegram user, links.

    The state is PEEKED, not consumed: consumption is the Confirm's, one-shot.
    Only a private chat is served — the confirmation names an account, and a
    group is not where it may be named — and in a private chat the chat IS the
    sender, which the check below holds to.

    Returns no reply text on any refusal: refusal copy is the router's, so a
    consumed token and an unknown one read identically to a prober (`07` §5).
    Every distinguishing detail rides the named ``outcome`` into the log.
    """
    if ctx.chat_type != "private" or ctx.chat_id != ctx.telegram_user_id:
        logger.warning("identity link: opened in a %s chat; refused", ctx.chat_type)
        return StartResult(outcome="not_private", handled=False)
    try:
        row = await oauth_states.peek_live_state(
            conn,
            state=ctx.payload,
            expected_purpose=PURPOSE,
            expected_provider=PROVIDER,
        )
    except oauth_states.OAuthStateRefused as exc:
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

    label = await account_label(conn, user_id=str(user_id))
    return StartResult(
        outcome="confirmation_offered",
        handled=True,
        reply=confirmation_text(label),
        reply_markup=confirmation_keyboard(ctx.payload, ctx.telegram_user_id),
    )


@dataclass(frozen=True)
class LinkTapOutcome:
    """What a confirmation tap ends in: the named outcome (the log), the
    tapper's toast or alert, and — when the prompt itself should change — the
    text that replaces it, its buttons gone. ``edit_text`` None leaves the
    prompt alone (a tap this lane will not act on is not its to rewrite)."""

    outcome: str
    answer_text: str
    show_alert: bool = False
    edit_text: Optional[str] = None


async def handle_tap(
    conn,
    tap: callback_tokens.LinkTap,
    *,
    from_user_id: Optional[str],
    chat_ref: Optional[str],
    chat_type: Optional[str],
    display_name: Optional[str] = None,
) -> LinkTapOutcome:
    """Step two: a Confirm or Cancel tap on the prompt :func:`handle_link` sent.

    Three gates before the state is touched, all answered with the router's
    one refusal and the prompt left as it is:

    - the prompt is in a private chat, and the chat is the tapper's own
      (in a private chat the chat id IS the user id);
    - the tapper is the Telegram user the prompt was offered to — the id the
      bot minted into the button, so the prompt confirms for that user only.

    Then Confirm consumes the state one-shot (:func:`oauth_states.consume_state`
    — a second Confirm, or a Confirm after Cancel or after expiry, is refused
    by the CAS) and links; Cancel consumes it too, so a cancelled link can
    never be confirmed.
    """
    if (
        chat_type != "private"
        or from_user_id is None
        or str(from_user_id) != str(chat_ref)
        or str(from_user_id) != tap.telegram_user_id
    ):
        logger.warning(
            "identity link tap refused: not the private chat of the user the"
            " confirmation was offered to"
        )
        return LinkTapOutcome("tapper_mismatch", REFUSAL, show_alert=True)

    try:
        row = await oauth_states.consume_state(
            conn,
            state=tap.state,
            expected_purpose=PURPOSE,
            expected_provider=PROVIDER,
        )
    except oauth_states.OAuthStateRefused as exc:
        logger.warning("identity link %s refused: %s", tap.action, exc)
        # The prompt is left as it is: a second tap on a prompt whose Confirm
        # already landed must not overwrite "now linked" with a refusal (the
        # two edits run after their commits, in no fixed order).
        return LinkTapOutcome("state_refused", REFUSAL, show_alert=True)

    if tap.action == callback_tokens.LINK_CANCEL:
        logger.info("identity link cancelled by the tapper; the state is spent")
        return LinkTapOutcome("cancelled", "Cancelled.", edit_text=CANCELLED)

    user_id = row["user_id"]
    if user_id is None:
        logger.error("identity link: link state with NULL user_id — CHECK missing?")
        return LinkTapOutcome("state_without_user", REFUSAL, True, edit_text=REFUSAL)

    try:
        created = await identity.link_identity(
            conn,
            user_id=str(user_id),
            provider=PROVIDER,
            external_id=str(from_user_id),
            display_name=display_name,
        )
    except identity.IdentityAlreadyLinked as exc:
        logger.warning("identity link refused: %s", exc.reason)
        return LinkTapOutcome(
            exc.reason,
            ALREADY_LINKED_ELSEWHERE,
            True,
            edit_text=ALREADY_LINKED_ELSEWHERE,
        )

    label = await account_label(conn, user_id=str(user_id))
    done = f"✅ This Telegram account is now linked to the Storydump account {label}."
    return LinkTapOutcome(
        "linked" if created else "already_linked", "Linked.", edit_text=done
    )


def register(router) -> None:
    """Wire this lane into the shared door."""
    router.register(PREFIX, handle_link)
