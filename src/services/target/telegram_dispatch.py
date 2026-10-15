"""The ingress dispatcher — the `/start` door, the group join path
and a group's move to a supergroup.

Three things are served, and the bound is still worth stating:

- **`/start <payload>`** — `link-` (identity: the `/start` only asks, and the
  prompt's Confirm tap — a `callback_query` served beside the cards' taps —
  links) and `bind-` (a group joins a workspace); `build_router` registers
  those two lanes and no other. The `inv-` lane (an invitation) is designed
  in `start_router.py` and not registered — an invitation is accepted on the
  web. The payload carries its own resolution, so these need no
  resolver.
- **A message in a group** — the Telegram join path: the people the bot
  can see (the sender; the people a `new_chat_members` service message names)
  become members of the workspace the group is bound to, through the
  `fn_group_member_seen` door, which reads the group's binding as a definer,
  as the resolver door does.
- **A group's migration notice** — Telegram retired the group's chat id for a
  supergroup's; the binding follows it (`chat_migration`), before anything
  from the new id has to resolve through it.

**Not served: chat-inbound COMMANDS.** An "approve" typed in a group is not
dispatched here; the resolver door exists (`fn_resolve_binding`), so that
is a dispatch question, not a resolution one.

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
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy.exc import SQLAlchemyError

from src.exceptions.tenancy import TenantResolutionError
from src.config.settings import settings
from src.services.target import (
    auto_approval,
    rate_counters,
    callback_tokens,
    channel_bind,
    chat_migration,
    commands,
    identity,
    identity_link,
    membership_sync,
    prompts,
    signup_admissions,
    tenant_resolution,
    unit_of_work,
)
from src.services.target.commands import Command, CommandRefused, CommandResult
from src.services.target.start_router import StartResult, StartRouter

#: How long a tap waits on another tap's row lock.
TAP_LOCK_TIMEOUT = "2s"

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# The tap
# ---------------------------------------------------------------------------

#: `rate_counters` scope for tap admission (`machinery.py` `ck_rate_scope`); the
#: window is a minute.
ADMISSION_SCOPE = "ws_admission"
ADMISSION_WINDOW_SECONDS = 60


class _AdmissionExhausted(Exception):
    """The window filled between the read and the debit (two taps raced)."""


#: A button's action → the command it runs.
ACTION_TO_COMMAND = {
    "post": "approve",
    "posted": "mark_posted",
    "skip": "skip",
    "reject": "reject",
    # The review card: one command, the resolution in `args`.
    "itposted": "resolve_review",
    "notposted": "resolve_review",
    "giveup": "resolve_review",
}
RESOLUTION_OF = {"itposted": "posted", "notposted": "retry", "giveup": "cancel"}
#: The verdict a review button's label carries: "🔁 Not there — post again"
#: is the member saying the story is not on Instagram, which `resolve_review`
#: requires before it re-approves an intent whose publish answer was lost.
VERDICT_OF = {"notposted": "not_posted"}

#: The outcomes a tap can end in that are not a command refusal reason or a
#: resolver refusal reason. Every one has an entry in ANSWERS.
TAP_OUTCOMES = (
    "executed",
    "answered",
    "menu",
    "older_card",
    "no_message",
    "unlinked",
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
    "menu": ("", False),  # navigation answers with no toast
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
    "nothing_to_confirm": (
        "Instagram didn't post this one — it was never asked, or it answered no —"
        " so there is nothing to confirm. Tap 🔁 Not there — post again, or 🚫 Give up.",
        True,
    ),
    "may_have_posted": (
        "Instagram may have posted this one. Check your story: tap ✅ It posted"
        " if it's there, or 🔁 Not there — post again if it isn't.",
        True,
    ),
    "too_many": ("Too many actions at once — try again in a minute.", True),
    "not_found": ("That post is gone.", True),
    # A Configure tap on a card that cannot post again: a rejected story's or
    # a removed item's (the menu heals it), or an item removed between the
    # menu's read and the flag's write (`set_item_auto_approve` refuses it as
    # `locked`).
    "locked": ("This item can't post again.", False),
    # The archive's selected media is not authorized or ready now.
    "media_unsuitable": (f"This post's media isn't ready to use — {_WEB}.", True),
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
        if data.get("dry_run") and data.get("paused"):
            return "✅ Approved — dry run, held while posting is paused"
        if data.get("dry_run"):
            return "✅ Approved — dry run, nothing will be published"
        if data.get("paused"):
            return "✅ Approved — posting is paused; it posts when you resume (within 3 days)"
        return "✅ Approved — posting shortly"
    if action == "posted" or action == "itposted":
        return "✅ Marked posted"
    if action == "notposted":
        if data.get("dry_run"):
            return "🔁 Posting again — dry run, nothing will be published"
        if data.get("paused"):
            return "🔁 Posting again when you resume — posting is paused"
        return "🔁 Posting again shortly"
    if action == "giveup":
        return "🚫 Cancelled"
    if action == "skip":
        days = data.get("lock_days")
        return f"⏭️ Skipped for {days} days" if days else "⏭️ Skipped"
    if action == "reject":
        return "🚫 Rejected"
    return "Done."


def _answered_text(result: CommandResult) -> str:
    """A tap on a card already decided answers with the card's own line, the
    `outcome_text` `command_executors._settle` writes, so the toast and the
    card never disagree — the rule that approved it included."""
    data = result.data or {}
    if data.get("outcome_text"):
        return f"Already: {data['outcome_text']}"
    state = data.get("state")
    word = (
        prompts.outcome_word(state, data.get("published_via")) if state else "decided"
    )
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
    if key == "manual_mode" and action in RESOLUTION_OF:
        # The approval card's answer names ✅ Posted myself; the review card
        # has no such button — its lever for a workspace that posts by hand
        # now is to give up here and post the story itself.
        return (
            "This workspace posts by hand now — tap 🚫 Give up here and post"
            " the story on Instagram yourself.",
            True,
        )
    return ANSWERS.get(key, FALLBACK_ANSWER)


def _chat_channel(message: dict) -> str:
    """The binding channel a Telegram chat resolves under: a private chat is
    `telegram_dm`; a group, supergroup or channel is `telegram_group` —
    `channel_bindings.channel`'s vocabulary (`tenant_resolution.CHAT_CHANNELS`)."""
    chat_type = ((message.get("chat") or {}).get("type") or "").lower()
    return "telegram_dm" if chat_type == "private" else "telegram_group"


def _command_for(
    tap: callback_tokens.Tap,
    tenant: tenant_resolution.ResolvedTenant,
    *,
    user_id: str,
    actor_label: Optional[str],
    card_ref: str,
) -> Command:
    """The `Command` a parsed tap runs, built from the button's action and the
    resolved chat — the middle step of `_tap`, kept out of its body so the
    tap's sequence reads in one sitting.
    Nothing here reads the database or decides anything: every gate is the
    executors' own."""
    args: dict[str, Any] = {"intent_id": tap.intent_id}
    if tap.action in RESOLUTION_OF:
        args["resolution"] = RESOLUTION_OF[tap.action]
    if tap.action in VERDICT_OF:
        args["verdict"] = VERDICT_OF[tap.action]
    return Command(
        kind=ACTION_TO_COMMAND[tap.action],
        workspace_id=tenant.workspace_id,
        actor_user_id=user_id,
        channel="telegram",
        args=args,
        # The tapper's name rides with the command — on its own
        # field, never in `args` — so the outcome line needs no
        # second identity read.
        actor_label=actor_label,
        # The card this tap came from: a settled story's
        # answer adopts a message the ledger never learned — a resend's
        # twin — and edits it, so no card keeps dead buttons.
        binding_id=tenant.channel_binding_id,
        card_ref=card_ref,
    )


#: The command a card menu's toggle runs; its floor is the menu's gate,
#: which navigation passes too.
MENU_COMMAND = "set_item_auto_approve"

#: An edit that clears a message's buttons.
_NO_KEYBOARD: dict = {"inline_keyboard": []}

#: What still holds a flagged item back, by `auto_approval.decide`'s block.
_HELD_BACK = {
    "api_off": " (once Instagram API posting is on)",
    "no_login": " (once Instagram is connected for this account)",
    "link": " (items with a link to add still ask)",
}


async def _flag_answer(conn, workspace_id: str, intent_id: str, on: bool) -> str:
    """What the toggle answers: the flag, and what still holds it back as the
    serve step would decide it now (`auto_approval.decide`, read after the
    flag's write) — the first block it names, as a waiting card's note does —
    so the answer never promises what the sweep would not do."""
    if not on:
        return "Auto-approve off"
    found = await auto_approval.facts(
        conn, workspace_id=workspace_id, intent_id=intent_id
    )
    # The next serve is another story: this one's pending cancel is no
    # business of its, and `decide` names no block at all for a cancel.
    held = (
        auto_approval.decide(found._replace(cancel_requested=False)).blocked_by
        if found is not None
        else None
    )
    return (
        "🔁 Auto-approve on: next time this item is served, it posts without a tap"
        + _HELD_BACK.get(held, "")
    )


@dataclass(frozen=True)
class TapResult:
    """What a served `callback_query` leaves for the route: the outcome (for
    the log and the counters), the query to answer and the card it came from
    (`chat_ref`/`message_ref` — the no-message gate reads them, and so does
    the route's one edit, `edit_text` or `edit_markup`). The route answers
    AFTER the commit, best effort; the transaction is the ack. `reply`
    exists so `_acknowledge`'s `/start` branch reads False."""

    outcome: str
    handled: bool
    callback_query_id: Optional[str]
    chat_ref: Optional[str]
    message_ref: Optional[str]
    answer_text: str
    show_alert: bool = False
    reply: Optional[str] = None
    #: Text that replaces the tapped message, its buttons removed — the
    #: identity link's confirmation prompt, or a waitlist ping its Admit
    #: settled — edited by the route after the commit, best effort. A
    #: card's outcome edit is the outbox's, never this.
    edit_text: Optional[str] = None
    #: The keyboard the tapped card's Configure menu leaves: the
    #: menu, Back to the card's keyboard, or none. One edit by the route after
    #: the commit, best effort; never through the outbox or the ledger.
    edit_markup: Optional[dict] = None


# ---------------------------------------------------------------------------
# The waitlist ping's ✅ Admit: user-plane, for the operators
# ---------------------------------------------------------------------------

#: An Admit tap by anyone but a linked user in `OPS_USER_IDS`.
NOT_OPERATOR = "not_operator"

#: An Admit tap's outcome → (what the operator reads, whether as an alert):
#: `signup_admissions.admit`'s answers (it is never asked to resend), and the
#: tap's own.
ADMIT_ANSWERS: dict[str, tuple[str, bool]] = {
    "admitted": ("✅ Admitted — the email is queued", False),
    "admitted_before": ("Already admitted", False),
    "signed_up": ("Already signed up", False),
    "invalid_email": ("This address can't be admitted", True),
    "not_configured": ("Can't admit: WEB_APP_URL isn't set on the API", True),
    "not_found": ("This signup is no longer on the waitlist", True),
    NOT_OPERATOR: ("Only the site's operators can admit", True),
    "tap_failed": ("Something went wrong on our side — try again in a moment", True),
}

#: The line a settled ping gains under its own text, its button gone.
ADMITTED_BY = "✅ Admitted by {}"
ALREADY_ADMITTED = "Already admitted"
ALREADY_SIGNED_UP = "Already signed up"


async def _admit(conn, from_id, ref: str) -> tuple[str, Optional[str]]:
    """An Admit tap's gates, in order, as (outcome, the line its ping gains).

    1. The tapper is a linked user in `OPS_USER_IDS`, asked as `require_ops`
       asks it (`Settings.is_ops`), whose account is active (the read that
       picks the ping's recipients); anyone else is `not_operator`, and
       nothing is written.
    2. The ref finds its entry (`fn_waitlist`); one removed by hand is
       `not_found`.
    3. `signup_admissions.admit` on this connection, so the admission and its
       email's job commit with the delivery; with no web origin for the claim
       link it answers `not_configured`.
    """
    tapper = (
        None
        if from_id is None
        else await identity.tapper_for_identity(
            conn, provider=identity.PROVIDER_TELEGRAM, external_id=str(from_id)
        )
    )
    if tapper is None or not settings.is_ops(tapper[0]):
        return NOT_OPERATOR, None
    # Active, as the ping's recipients are: a disabled account still named in
    # `OPS_USER_IDS` cannot admit from an older ping.
    if str(from_id) not in await identity.telegram_ids_for(conn, [tapper[0]]):
        return NOT_OPERATOR, None
    found = await signup_admissions.waitlist(conn, ref=ref)
    if not found:
        return "not_found", None
    # One savepoint for the admission and its email's job, so nothing past the
    # door can leave the delivery committing an admission without its email.
    async with conn.begin_nested():
        outcome = await signup_admissions.admit(
            conn,
            email=found[0]["email"],
            claim_base=signup_admissions.claim_base(settings.web_app_origin),
        )
    if outcome == "admitted":
        user_id, label = tapper
        # A tapper's name, else the long way, as a card tap's outcome line.
        name = label or await identity.display_name_for(conn, user_id=user_id)
        return outcome, ADMITTED_BY.format(name)
    if outcome == "admitted_before":
        return outcome, ALREADY_ADMITTED
    if outcome == "signed_up":
        return outcome, ALREADY_SIGNED_UP
    return outcome, None


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

    Registration is how a lane joins the door; a second door would break
    the lanes' disjointness. The `inv-` lane is not registered: nothing
    below serves it, and the docstring at the top says so.
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
        moved = chat_migration.migration_of(payload)
        if moved is not None:
            old_ref, new_ref = moved
            return await chat_migration.follow(conn, old_ref=old_ref, new_ref=new_ref)
        start_payload = StartRouter.payload_of(payload)
        people = membership_sync.group_members_of(payload)
        if people and (start_payload is None or start_payload == ""):
            # The Telegram join path. A BARE `/start` in a group
            # is speech, not a greeting request: the greeting is the DM's, and
            # in a group it would be a lever anyone could pull to make the bot
            # talk. Named outcomes, no reply, and never a raise.
            return await self._observe_all(conn, people)
        if start_payload is None:
            logger.info(
                "ingress: update is neither a /start command nor a group message;"
                " not served here; chat-inbound commands are not dispatched (#854)"
            )
            return StartResult(outcome=NOT_A_START, handled=False)
        result = await self.router.dispatch(conn, payload)
        logger.info("ingress: /start dispatched, outcome=%s", result.outcome)
        return result

    async def _tap(self, conn, payload: dict) -> TapResult:
        """A button tap on a card: parse → resolve the chat →
        resolve the tapper → the command port, as the tapping member, on this
        connection with the actor GUCs set. Every refusal is an ANSWER; only a
        database error escapes (the route maps it). Nothing here speaks to
        Telegram — the route answers after the commit. A card command's edit
        is the outbox's; a menu tap (`_menu`) leaves its keyboard for the
        route's one edit (`TapResult.edit_markup`). The identity link's
        prompt and the waitlist ping are no card: their taps go first, to the
        user-plane `_link_tap` and `_admit_tap`."""
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
            answer: Optional[tuple[str, bool]] = None,
            edit_markup: Optional[dict] = None,
        ) -> TapResult:
            text, alert = answer or answer_for(
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
                edit_markup=edit_markup,
            )

        link_tap = callback_tokens.parse_link(cq.get("data"))
        if link_tap is not None:
            return await self._link_tap(conn, payload, link_tap, started)
        admit_tap = callback_tokens.parse_admit(cq.get("data"))
        if admit_tap is not None:
            return await self._admit_tap(conn, payload, admit_tap, started)

        try:
            tap = callback_tokens.parse(cq.get("data"))
            if tap is None:
                return done("older_card")
            action = tap.action
            if chat_ref is None or message_ref is None:
                # Inline mode, or a message Telegram no longer shows us: there
                # is no card to act on.
                return done("no_message")
            try:
                tenant = await tenant_resolution.resolve_chat(
                    conn, _chat_channel(message), chat_ref
                )
            except TenantResolutionError as exc:
                return done(exc.reason)
            workspace = tenant.workspace_id
            from_id = (cq.get("from") or {}).get("id")
            tapper = (
                None
                if from_id is None
                else await identity.tapper_for_identity(
                    conn, provider="telegram", external_id=str(from_id)
                )
            )
            if tapper is None:
                return done("unlinked")
            user_id, actor_label = tapper
            # One statement for the tenant and actor GUCs AND the lock
            # timeout: a tap waits on another tap's row lock for at most 2 s;
            # past it the database refuses, the route answers 5xx and Telegram
            # redelivers — read-then-decide makes the retry safe, and no
            # convoy can hold the ingress pool hostage.
            await unit_of_work.apply_gucs(
                conn,
                tenant_id=tenant.workspace_id,
                actor_kind="user",
                actor_user_id=str(user_id),
                channel="telegram",
                lock_timeout=TAP_LOCK_TIMEOUT,
            )
            limit = int(settings.TARGET_TAP_ADMISSION_PER_MINUTE)
            window = rate_counters.window_start(
                datetime.now(timezone.utc), ADMISSION_WINDOW_SECONDS
            )
            try:
                if tap.action in prompts.MENU_ACTIONS:
                    outcome, answer, markup = await self._menu(
                        conn, tap, tenant, str(user_id), actor_label, window, limit
                    )
                    return done(outcome, answer=answer, edit_markup=markup)
                command = _command_for(
                    tap,
                    tenant,
                    user_id=str(user_id),
                    actor_label=actor_label,
                    card_ref=message_ref,
                )
                # Tap admission: the workspace's window is DEBITED inside
                # `_execute_with_debit`'s savepoint, only for a flip that ran —
                # the increment's own `WHERE count < limit` is the check,
                # atomic under the row lock, so at the limit the flip rolls
                # back with the savepoint and the tapper is told; nothing is
                # spent, the delivery is consumed (a repeat is a fresh update).
                # No read runs before the flip: a repeat tap on a settled card
                # is answered with its state even at the cap, because
                # `_settle` answers before any write.
                result = await self._execute_with_debit(
                    conn, command, tenant, window, limit
                )
            except (TenantResolutionError, CommandRefused) as exc:
                return done(exc.reason)
            except _AdmissionExhausted:
                return done("too_many")
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

    async def _link_tap(
        self, conn, payload: dict, link_tap: callback_tokens.LinkTap, started: float
    ) -> TapResult:
        """Confirm or Cancel on the identity link's prompt — the `link-`
        lane's second step, user-plane like its first: no chat resolution, no
        tenant, no command port and no admission debit (no workspace exists to
        charge). Every gate is :func:`identity_link.handle_tap`'s; this only
        reads the update. A database error escapes to the route as a card
        tap's does; anything else is a named `tap_failed`."""
        cq: dict[str, Any] = payload["callback_query"]
        qid = None if cq.get("id") is None else str(cq.get("id"))
        message = cq.get("message") if isinstance(cq.get("message"), dict) else {}
        chat = message.get("chat") or {}
        sender = cq.get("from") or {}
        chat_ref = None if chat.get("id") is None else str(chat.get("id"))
        message_id = message.get("message_id")
        message_ref = None if message_id is None else str(message_id)
        try:
            outcome = await identity_link.handle_tap(
                conn,
                link_tap,
                from_user_id=None if sender.get("id") is None else str(sender["id"]),
                chat_ref=chat_ref,
                chat_type=chat.get("type"),
                display_name=sender.get("username") or sender.get("first_name"),
            )
        except SQLAlchemyError:
            raise
        except Exception:  # noqa: BLE001 — a poisoned update must be a NAMED outcome
            logger.exception(
                "link tap failed (update_id=%s) — answered as tap_failed, delivery kept",
                payload.get("update_id"),
            )
            text, alert = answer_for("tap_failed")
            outcome = identity_link.LinkTapOutcome("tap_failed", text, alert)
        logger.info(
            "link tap update_id=%s action=%s outcome=%s dispatch_ms=%d",
            payload.get("update_id"),
            link_tap.action,
            outcome.outcome,
            int((time.monotonic() - started) * 1000),
        )
        return TapResult(
            outcome=outcome.outcome,
            handled=True,
            callback_query_id=qid,
            chat_ref=chat_ref,
            message_ref=message_ref,
            answer_text=outcome.answer_text,
            show_alert=outcome.show_alert,
            edit_text=outcome.edit_text,
        )

    async def _admit_tap(
        self, conn, payload: dict, admit_tap: callback_tokens.AdmitTap, started: float
    ) -> TapResult:
        """✅ Admit on a waitlist ping, user-plane like `_link_tap`: no
        chat resolution, no tenant, no command port and no admission debit
        (no workspace exists to charge). The gates are `_admit`'s, and every
        outcome is an answer from `ADMIT_ANSWERS`. Once the entry is admitted,
        by this tap or before it, the ping is edited: its own text and one
        line, its button gone. A database error escapes to the route as a card
        tap's does; anything else is a named `tap_failed`. The log names the
        outcome and the update, never the address, the ref or a chat id."""
        cq: dict[str, Any] = payload["callback_query"]
        qid = None if cq.get("id") is None else str(cq.get("id"))
        message = cq.get("message") if isinstance(cq.get("message"), dict) else {}
        chat_id = (message.get("chat") or {}).get("id")
        message_id = message.get("message_id")
        try:
            outcome, line = await _admit(
                conn, (cq.get("from") or {}).get("id"), admit_tap.ref
            )
        except SQLAlchemyError:
            raise
        except Exception:  # noqa: BLE001 — a poisoned update must be a NAMED outcome
            logger.exception(
                "admit tap failed (update_id=%s) — answered as tap_failed, delivery kept",
                payload.get("update_id"),
            )
            outcome, line = "tap_failed", None
        logger.info(
            "admit tap update_id=%s outcome=%s dispatch_ms=%d",
            payload.get("update_id"),
            outcome,
            int((time.monotonic() - started) * 1000),
        )
        text, alert = ADMIT_ANSWERS.get(outcome, ADMIT_ANSWERS["tap_failed"])
        ping = message.get("text")
        return TapResult(
            outcome=outcome,
            handled=True,
            callback_query_id=qid,
            chat_ref=None if chat_id is None else str(chat_id),
            message_ref=None if message_id is None else str(message_id),
            answer_text=text,
            show_alert=alert,
            # The ping's own words and the line; no text from Telegram (a
            # message it no longer shows us) leaves the ping alone.
            edit_text=f"{ping}\n{line}" if line and ping else None,
        )

    async def _menu(
        self, conn, tap, tenant, user_id: str, actor_label, window, limit: int
    ) -> tuple[str, tuple[str, bool], dict]:
        """A Configure tap: open the menu, go Back, or turn the item's
        Auto-approve flag. Behind the gate the flag's command applies, and the
        tap's admission is spent, so one workspace's burst of menu taps is
        capped (`TARGET_TAP_ADMISSION_PER_MINUTE`), like its other taps that
        write. The answer is (outcome, (text, alert), the keyboard the edit
        leaves). The toggle decides nothing about the card it was tapped on:
        the flag acts the next time the item is served. A card whose
        story carries no keyboard — a rejected story's, a removed item's —
        heals on touch, its buttons gone."""
        await commands.authorize(
            conn, MENU_COMMAND, workspace_id=tenant.workspace_id, actor_user_id=user_id
        )
        row = await prompts.card_facts(
            conn, workspace_id=tenant.workspace_id, intent_id=tap.intent_id
        )
        if row is None:
            raise CommandRefused("not_found", f"intent {tap.intent_id}")
        card = prompts.keyboard_of(tap.intent_id, row)
        if card is None:
            await self._spend(conn, tenant.workspace_id, window, limit)
            return "menu", ANSWERS["locked"], _NO_KEYBOARD
        if tap.action in ("aa1", "aa0"):
            on = tap.action == "aa1"
            command = Command(
                kind=MENU_COMMAND,
                workspace_id=tenant.workspace_id,
                actor_user_id=user_id,
                channel="telegram",
                args={"media_item_id": str(row["media_item_id"]), "auto_approve": on},
                actor_label=actor_label,
            )
            await self._execute_with_debit(conn, command, tenant, window, limit)
            answer = await _flag_answer(conn, tenant.workspace_id, tap.intent_id, on)
            menu = prompts.menu_keyboard(
                tap.intent_id, on=on, settings_url=prompts.settings_url()
            )
            return "executed", (answer, False), menu
        await self._spend(conn, tenant.workspace_id, window, limit)
        if tap.action == "cfg":
            menu = prompts.menu_keyboard(
                tap.intent_id,
                on=bool(row["auto_approve"]),
                settings_url=prompts.settings_url(),
            )
            return "menu", ANSWERS["menu"], menu
        return "menu", ANSWERS["menu"], card

    async def _execute_with_debit(
        self, conn, command: Command, tenant, window, limit: int
    ) -> CommandResult:
        """Run the command and spend the workspace's admission for it, both
        inside ONE savepoint. Every refusal leaves through the caller's
        `except` arms: the savepoint unwinds here, in this frame, before the
        exception reaches them.

        A savepoint: a refusal the database raised mid-executor (the guard's
        last line; `publish_cap`'s manual-post flip) must not leave the
        admission's transaction aborted — the route's COMMIT would silently
        become a ROLLBACK and the delivery would be lost with a 200.
        """
        begin_nested = getattr(conn, "begin_nested", None)
        if callable(begin_nested):
            async with begin_nested():
                result = await commands.execute(conn, command)
                await self._debit(conn, tenant.workspace_id, window, limit, result)
        else:
            result = await commands.execute(conn, command)
            await self._debit(conn, tenant.workspace_id, window, limit, result)
        return result

    @staticmethod
    async def _debit(conn, workspace_id: str, window, limit: int, result) -> None:
        """Spend one unit of the workspace's tap admission for a flip that RAN
                (`executed`, or `enqueued` — a `post` that minted its job), and for an
                `answered` tap that ADOPTED a card, which writes a row and a paced edit.
        An `answered` no-op and a refusal spend nothing. Raises
                :class:`_AdmissionExhausted` when the window is full — inside the
                caller's savepoint, so the flip rolls back with it."""
        outcome = getattr(result, "outcome", None)
        wrote = outcome in ("executed", "enqueued") or (
            outcome == "answered"
            and bool((getattr(result, "data", None) or {}).get("adopted"))
        )
        if not wrote:
            return
        await TelegramDispatcher._spend(conn, workspace_id, window, limit)

    @staticmethod
    async def _spend(conn, workspace_id: str, window, limit: int) -> None:
        """One unit of the workspace's tap admission, or
        :class:`_AdmissionExhausted` when the window is full."""
        n = await rate_counters.increment(
            conn,
            scope=ADMISSION_SCOPE,
            key=workspace_id,
            window_start=window,
            limit=limit,
        )
        if n is None:
            raise _AdmissionExhausted()

    async def _observe_all(self, conn, people) -> StartResult:
        """Every person the message showed the bot; the result is the first
        one that joined, else the last outcome, so a log reader sees the
        interesting event. Steady-state outcomes log at DEBUG — a chatty group
        would otherwise fill the log with `already_member`. A failure is the
        result whatever else the message held: `membership_sync_failed`.

        Each person is observed in a savepoint of their own, for the reason
        :meth:`_execute_with_debit` gives: the error is swallowed here, so it
        must not leave the admission's transaction aborted. The failed join
        rolls back alone, and the people after it are still observed."""
        result = StartResult(outcome=NOT_A_START, handled=False)
        failed = False
        for chat_type, external_ref, telegram_user_id in people:
            try:
                async with conn.begin_nested():
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
                failed = True
                continue
            logger.log(
                logging.INFO if seen.handled else logging.DEBUG,
                "ingress: group message observed, outcome=%s",
                seen.outcome,
            )
            # One rule, not two: `seen.handled and not result.handled`
            # implies `not result.handled`, so this one branch suffices.
            if not result.handled:
                result = seen
        if failed:
            return StartResult(outcome=MEMBERSHIP_SYNC_FAILED, handled=False)
        return result
