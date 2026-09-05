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
from typing import Optional

from src.services.target import channel_bind, identity_link, membership_sync
from src.services.target.start_router import StartResult, StartRouter

logger = logging.getLogger(__name__)

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

    async def __call__(self, conn, payload: dict) -> StartResult:
        """Dispatch one admitted delivery. Never raises for an unservable
        update — the delivery is already admitted, so raising would strand it.
        """
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
