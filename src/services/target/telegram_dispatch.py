"""The ingress dispatcher — #1183, the `/start` door ONLY.

## ⚠ What this does NOT enable

**Wiring this does not make chat-inbound work.** A `/start` payload carries its
own resolution — `link-<state>` names its user, `inv-<token>` names its
workspace — so both resolve against tables `svc_ingress` can already reach
pre-context. **An ordinary chat message carries neither**, and can only be
attributed by resolving ``(channel, external_ref) → workspace_id``, which needs
the resolver door proposed in **#854**. That issue is unchanged by this one.

*"Ingress is wired"* is what gets remembered; *"for the `/start` door only"* is
what gets dropped. Hence this paragraph, and hence the refusal below being
named rather than silent.

## Why a non-`/start` update is a NAMED outcome and never a silent drop

A dispatcher that silently ignores what it cannot handle is
**indistinguishable from one that had nothing to do.** Both produce no error,
no log line worth reading, and no signal. That is exactly what would make the
bound above undetectable: chat-inbound would look wired, do nothing, and say
nothing, with #854 still open and no way to tell from the outside.

So every update this cannot serve leaves a named `outcome` and a log line.
Silence here is a defect, not tidiness.
"""

from __future__ import annotations

import logging
from typing import Optional

from src.services.target import identity_link
from src.services.target.start_router import StartResult, StartRouter

logger = logging.getLogger(__name__)

#: Not a `/start` at all — an ordinary message, an edit, a callback query.
#: Served by nothing here; that is #854's path, and it is SAID rather than
#: dropped.
NOT_A_START = "not_a_start"


def build_router() -> StartRouter:
    """The one `/start` door, with every lane registered into it.

    Lane C registers `inv-` here too (#1172). Registration is how a lane joins
    the door; a second door would break D33/D35's disjointness.
    """
    router = StartRouter()
    identity_link.register(router)
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
        if StartRouter.payload_of(payload) is None:
            # NOT a silent drop. See the module docstring: this line is what
            # keeps the /start-only bound observable from the outside.
            logger.info(
                "ingress: update is not a /start command; not served here "
                "(chat-inbound resolution is #854, still open)"
            )
            return StartResult(outcome=NOT_A_START, handled=False)

        result = await self.router.dispatch(conn, payload)
        logger.info("ingress: /start dispatched, outcome=%s", result.outcome)
        return result
