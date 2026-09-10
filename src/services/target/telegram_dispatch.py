"""The ingress dispatcher — the `/start` door (#1183) and the group join path (#1242).

Two things are served, and the bound is still worth stating:

- **`/start <payload>`** — `link-` (identity), `bind-` (a group joins a
  workspace) and `inv-` (an invitation). The payload carries its own
  resolution, so these never needed a resolver.
- **A message in a group** — the `06` Telegram join path: the people the bot
  can see (the sender; the people a `new_chat_members` service message names)
  become members of the workspace the group is bound to, through the
  `fn_group_member_seen` door (`07` §14, built on #854's resolver door).

**Still not served: chat-inbound COMMANDS.** An "approve" typed in a group is
not dispatched here; the resolver door exists now (`fn_resolve_binding`), so
that is a dispatch question, not a resolution one — #854 stays open for it.

## Why an unservable update is a NAMED outcome and never a silent drop

A dispatcher that silently ignores what it cannot handle is
**indistinguishable from one that had nothing to do.** Both produce no error,
no log line worth reading, and no signal. So every update this cannot serve
leaves a named `outcome` and a log line. Silence here is a defect, not
tidiness — and a raise is worse: the delivery is admitted by the time this
runs, so an exception would roll the admission back and make Telegram
redeliver the same update forever.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from src.exceptions.tenancy import TenantResolutionError
from src.services.target import (
    callback_tokens,
    channel_bind,
    commands,
    identity,
    identity_link,
    membership_sync,
    prompts,
    tenant_resolution,
    unit_of_work,
)
from src.services.target.commands import Command, CommandRefused, CommandResult
from src.services.target.start_router import StartResult, StartRouter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# The tap (W4 — phase 1 of the 2026-09-09 plan)
# ---------------------------------------------------------------------------

#: Flipped by #1220 step 3 when the worker's publish leg is live. Until then
#: the `post` answer says what is true (F11): approval is recorded, publishing
#: is not yet automatic.
PUBLISH_LEG_LIVE = True

#: A button's action → the command it runs (phase 1 step 11).
ACTION_TO_COMMAND = {
    "post": "approve",
    "posted": "mark_posted",
    "skip": "skip",
    "reject": "reject",
}

#: The outcomes a tap can end in that are not a command refusal reason or a
#: resolver refusal reason. Every one has an entry in ANSWERS.
TAP_OUTCOMES = (
    "executed",
    "answered",
    "older_card",
    "no_message",
    "unlinked",
    "rate_limited",
    "tap_failed",
)

_WEB = "open the queue on the web"
FALLBACK_ANSWER = (f"Couldn't do that — {_WEB}.", True)

#: outcome / refusal reason → (what the tapper reads, whether as an alert).
#: `show_alert` is for what the tapper must ACT on; a toast otherwise.
ANSWERS: dict[str, tuple[str, bool]] = {
    # tap outcomes
    "executed": ("Done.", False),  # replaced per action by `answer_for`
    "answered": ("Already decided.", False),  # replaced by the card's state
    "older_card": (
        f"This button is from an older card — {_WEB} for the current one.",
        True,
    ),
    "no_message": (f"This card can't be acted on from here — {_WEB}.", True),
    "unlinked": (
        "Link your Telegram account first: sign in on the web and open"
        " Settings › Integrations → Link Telegram.",
        True,
    ),
    "rate_limited": ("Too many actions at once — try again in a minute.", True),
    "tap_failed": (
        f"Something went wrong on our side — try again in a moment, or {_WEB}.",
        True,
    ),
    # resolver refusals
    "unknown_binding": (
        "This chat isn't connected to a workspace — add it from Settings › Integrations on the web.",
        True,
    ),
    "revoked_binding": ("This chat is no longer connected to a workspace.", True),
    "unknown_channel": FALLBACK_ANSWER,
    "not_a_member": ("You're not a member of this workspace.", True),
    "insufficient_role": ("Your role in this workspace can't do that.", True),
    # command refusals (`commands.REASONS`)
    "manual_mode": (
        "This workspace posts by hand — post it on Instagram, then tap ✅ Posted myself.",
        True,
    ),
    "not_connected": (
        "Instagram isn't connected for this account yet — connect it in Settings ›"
        " Integrations, or post by hand and tap ✅ Posted myself.",
        True,
    ),
    "not_found": ("That post is gone.", True),
    "cancelling": ("This card is being cancelled.", False),
    "illegal_transition": ("That card can't take this action any more.", False),
    "invalid_args": (f"Couldn't read that button — {_WEB}.", True),
    "unknown_command": FALLBACK_ANSWER,
    "not_built": FALLBACK_ANSWER,
    "workspace_required": FALLBACK_ANSWER,
}


def _executed_text(action: Optional[str], result: CommandResult) -> str:
    data = result.data or {}
    if action == "post":
        if PUBLISH_LEG_LIVE:
            return "✅ Approved — posting shortly"
        return "✅ Approved — publishing isn't live yet; it will post when it is"
    if action == "posted":
        return "✅ Marked posted"
    if action == "skip":
        days = data.get("lock_days")
        return f"⏭️ Skipped for {days} days" if days else "⏭️ Skipped"
    if action == "reject":
        return "🚫 Rejected"
    return "Done."


def _answered_text(result: CommandResult) -> str:
    data = result.data or {}
    state = data.get("state")
    word = prompts.OUTCOME_WORDS.get(state, state or "decided")
    who = f" by {data['settled_by']}" if data.get("settled_by") else ""
    when = f" · {data['settled_at']}" if data.get("settled_at") else ""
    return f"Already: {word}{who}{when}"


def answer_for(
    key: str,
    *,
    result: Optional[CommandResult] = None,
    action: Optional[str] = None,
) -> tuple[str, bool]:
    """The answer a tap gets for an outcome or a refusal reason: (text, alert).
    Every reason the executors and the resolver can raise has an entry; an
    unknown one falls back to the web."""
    if key == "executed" and result is not None:
        return _executed_text(action, result), False
    if key == "answered" and result is not None:
        return _answered_text(result), False
    return ANSWERS.get(key, FALLBACK_ANSWER)


def _chat_channel(message: dict) -> str:
    """The binding channel a Telegram chat resolves under: a private chat is
    `telegram_dm`; a group, supergroup or channel is `telegram_group` —
    `channel_bindings.channel`'s vocabulary (`tenant_resolution.CHAT_CHANNELS`)."""
    chat_type = ((message.get("chat") or {}).get("type") or "").lower()
    return "telegram_dm" if chat_type == "private" else "telegram_group"


@dataclass(frozen=True)
class TapResult:
    """What a served `callback_query` leaves for the route: the outcome (for
    the log and the counters), the query to answer and the card to strip. The
    route answers AFTER the commit, best effort; the transaction is the ack
    (R5). `reply` exists so `_acknowledge`'s `/start` branch reads False."""

    outcome: str
    handled: bool
    callback_query_id: Optional[str]
    chat_ref: Optional[str]
    message_ref: Optional[str]
    answer_text: str
    show_alert: bool = False
    reply: Optional[str] = None


#: Neither a `/start` nor a group message — a DM that is not a command, an
#: edit, a callback query, a channel post. Served by nothing here, and SAID
#: rather than dropped.
NOT_A_START = "not_a_start"
#: The join path raised (a database error, a missing door): named, logged
#: with the traceback, and the delivery stays admitted so Telegram does not
#: redeliver it forever.
MEMBERSHIP_SYNC_FAILED = "membership_sync_failed"


def build_router() -> StartRouter:
    """The one `/start` door, with every lane registered into it.

    Lane C registers `inv-` here too (#1172). Registration is how a lane joins
    the door; a second door would break D33/D35's disjointness.
    """
    router = StartRouter()
    identity_link.register(router)
    channel_bind.register(router)
    return router


class TelegramDispatcher:
    """`IngressRuntime.dispatch` for the `/start` door.

    Constructed once at the composition root and closed over for the app's
    lifetime — the `IngressRuntime` docstring's rule, since it holds the
    router rather than per-request state.
    """

    def __init__(self, router: Optional[StartRouter] = None) -> None:
        self.router = router if router is not None else build_router()

    async def __call__(self, conn, payload: dict):
        """Dispatch one admitted delivery. Never raises for an unservable
        update — the delivery is already admitted, so raising would strand it.
        """
        if isinstance(payload.get("callback_query"), dict):
            return await self._tap(conn, payload)
        start_payload = StartRouter.payload_of(payload)
        people = membership_sync.group_members_of(payload)
        if people and (start_payload is None or start_payload == ""):
            # The `06` Telegram join path (#1242). A BARE `/start` in a group
            # is speech, not a greeting request: the greeting is the DM's, and
            # in a group it would be a lever anyone could pull to make the bot
            # talk. Named outcomes, no reply, and never a raise.
            return await self._observe_all(conn, people)
        if start_payload is None:
            logger.info(
                "ingress: update is neither a /start command nor a group message;"
                " not served here (chat-inbound commands are #854)"
            )
            return StartResult(outcome=NOT_A_START, handled=False)
        result = await self.router.dispatch(conn, payload)
        logger.info("ingress: /start dispatched, outcome=%s", result.outcome)
        return result

    async def _tap(self, conn, payload: dict) -> TapResult:
        """A button tap on a card (phase 1 step 9): parse → resolve the chat →
        resolve the tapper → the command port, as the tapping member, on this
        connection with the actor GUCs set. Every refusal is an ANSWER; only a
        database error escapes (the route maps it). Nothing here speaks to
        Telegram — the route answers and strips after the commit."""
        started = time.monotonic()
        cq: dict[str, Any] = payload["callback_query"]
        qid = cq.get("id")
        qid = None if qid is None else str(qid)
        message = cq.get("message") if isinstance(cq.get("message"), dict) else {}
        chat_id = (message.get("chat") or {}).get("id")
        message_id = message.get("message_id")
        chat_ref = None if chat_id is None else str(chat_id)
        message_ref = None if message_id is None else str(message_id)
        action: Optional[str] = None
        workspace: Optional[str] = None

        def done(
            outcome: str,
            *,
            result: Optional[CommandResult] = None,
            answer_key: Optional[str] = None,
        ) -> TapResult:
            text, alert = answer_for(
                answer_key or outcome, result=result, action=action
            )
            logger.info(
                "tap update_id=%s workspace=%s action=%s outcome=%s dispatch_ms=%d",
                payload.get("update_id"),
                workspace,
                action,
                outcome,
                int((time.monotonic() - started) * 1000),
            )
            return TapResult(
                outcome=outcome,
                handled=True,
                callback_query_id=qid,
                chat_ref=chat_ref,
                message_ref=message_ref,
                answer_text=text,
                show_alert=alert,
            )

        try:
            tap = callback_tokens.parse(cq.get("data"))
            if tap is None:
                return done("older_card")
            action = tap.action
            if chat_ref is None or message_ref is None:
                # Inline mode, or a message Telegram no longer shows us: there
                # is no card to act on and nothing to strip.
                return done("no_message")
            try:
                tenant = await tenant_resolution.resolve_chat(
                    conn, _chat_channel(message), chat_ref
                )
            except TenantResolutionError as exc:
                return done(exc.reason)
            workspace = tenant.workspace_id
            from_id = (cq.get("from") or {}).get("id")
            user_id = (
                None
                if from_id is None
                else await identity.user_for_identity(
                    conn, provider="telegram", external_id=str(from_id)
                )
            )
            if user_id is None:
                return done("unlinked")
            await unit_of_work.apply_gucs(
                conn,
                tenant_id=tenant.workspace_id,
                actor_kind="user",
                actor_user_id=str(user_id),
                channel="telegram",
            )
            # A tap waits on another tap's row lock for at most this long;
            # past it the database refuses, the route answers 5xx and Telegram
            # redelivers — read-then-decide makes the retry safe, and no
            # convoy can hold the ingress pool hostage (review of #1271).
            if hasattr(conn, "execute"):
                await conn.execute(text("SET LOCAL lock_timeout = '2s'"))
            command = Command(
                kind=ACTION_TO_COMMAND[tap.action],
                workspace_id=tenant.workspace_id,
                actor_user_id=str(user_id),
                channel="telegram",
                args={"intent_id": tap.intent_id},
            )
            try:
                # A savepoint: a refusal the database raised mid-executor
                # (the guard's last line; `mark_posted`'s debit CTE) must not
                # leave the admission's transaction aborted — the route's
                # COMMIT would silently become a ROLLBACK and the delivery
                # would be lost with a 200 (structural review of #1271).
                begin_nested = getattr(conn, "begin_nested", None)
                if callable(begin_nested):
                    async with begin_nested():
                        result = await commands.execute(conn, command)
                else:
                    result = await commands.execute(conn, command)
            except TenantResolutionError as exc:
                return done(exc.reason)
            except CommandRefused as exc:
                return done(exc.reason)
            outcome = "answered" if result.outcome == "answered" else "executed"
            return done(outcome, result=result)
        except SQLAlchemyError:
            raise  # the route's business: a database fault is not a tap outcome
        except Exception:  # noqa: BLE001 — a poisoned update must be a NAMED outcome
            logger.exception(
                "tap failed (update_id=%s) — answered as tap_failed, delivery kept",
                payload.get("update_id"),
            )
            return done("tap_failed")

    async def _observe_all(self, conn, people) -> StartResult:
        """Every person the message showed the bot; the result is the first
        one that joined, else the last outcome, so a log reader sees the
        interesting event. Steady-state outcomes log at DEBUG — a chatty group
        would otherwise fill the log with `already_member`."""
        result = StartResult(outcome=NOT_A_START, handled=False)
        for chat_type, external_ref, telegram_user_id in people:
            try:
                seen = await membership_sync.observe(
                    conn,
                    chat_type=chat_type,
                    external_ref=external_ref,
                    telegram_user_id=telegram_user_id,
                )
            except Exception:  # noqa: BLE001 — a poisoned update must not loop
                logger.exception(
                    "ingress: membership sync failed; the delivery stays admitted"
                )
                return StartResult(outcome=MEMBERSHIP_SYNC_FAILED, handled=False)
            logger.log(
                logging.INFO if seen.handled else logging.DEBUG,
                "ingress: group message observed, outcome=%s",
                seen.outcome,
            )
            if seen.handled and not result.handled:
                result = seen
            elif not result.handled:
                result = seen
        return result
