"""W4 — the Telegram webhook ingress route (#942, `02` §6).

The deployed half of L.8. `webhook_ingress` has supplied `verify_secret_token`,
`fingerprint` and `admit` since #865 and nothing has ever mounted them; this
route is that mount. It stops at the resolution seam: tenant resolution is
#854's ruling and its migration, and none of it is here.

## Why this half can land before that ruling

`admit()` takes `(conn, channel, external_ref, payload, principal)` and **no
tenant**. Admission keys on `(channel, principal, external_ref)`; resolution
happens afterwards, when something goes to *act* on the admitted command. The
idempotency boundary is upstream of the tenant boundary, so this half has no
dependency on the other.

## The ordering rule, which is the whole design

**A delivery that cannot be executed is refused BEFORE admission, never after.**

Admission is irreversible. The `command_dedup` key persists, so a later
redelivery of the same update — once the resolver exists — is a
`DeliveryReplayed` and is correctly *not* executed. Admitting a delivery we
cannot dispatch therefore does not defer the command, it **destroys** it, and it
destroys it wearing the shape of successful deduplication. That is the failure
`AdmissionConflict`'s docstring names as the invisible one.

This is the faithful translation of W1's parking discipline rather than a
departure from it. W1 parks an executor-less job by rescheduling it **alive** and
never finalizes it dead: the irreversible step there is finalization, and W1
declines to take it. The irreversible step here is admission, so this route
declines to take it, and the provider's own redelivery is the park cadence.

The rejected alternative was admit-then-park behind a 200. It wires admission
more visibly, and the loss is unreachable in production because the route is
dormant until `setWebhook`. Rejected because "unreachable in production" is a
property of an operational fact — nobody has registered the webhook — rather
than of the code, and the reason to land this early is precisely that the code
should be right before that fact changes.

**When the second channel arrives, this rule moves — it does not get retyped.**
`webhook_ingress.CHANNELS` already names `web` and `cli`, and each will need the
same refuse-before-admit ordering. At that point the ordering belongs in an
`ingest()` on the service, taking the connect/dispatch pair and returning a
typed outcome, with this route reduced to header, parse, and status mapping. It
is deliberately NOT extracted today, with one channel realized and the seam
still unwired, because the shape of the second caller is exactly what is not yet
known. The trigger for extracting it is the second caller, not a later reading
of this paragraph.

## Dormancy is enforced by configuration, not only by non-registration

`verify_secret_token` refuses when the expected value is absent, so a deployment
that has not set `TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN` refuses every delivery
at the door. Reaching admission requires setting the secret **and** registering
the webhook — two deliberate acts, neither of which this PR performs.

## Why `dispatch` receives the connection

So that #854 can make admission and effect **one transaction**. If the route
committed admission and then called a dispatcher on its own connection, a
dispatch failure would leave the key committed and the command unexecutable —
re-creating the loss this module exists to avoid, one layer down. Handing the
connection over leaves that choice where the knowledge is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from fastapi import APIRouter, HTTPException, Request

from src.config.settings import settings
from src.services.target.webhook_ingress import (
    AdmissionConflict,
    DeliveryReplayed,
    TELEGRAM_PRINCIPAL,
    admit,
    verify_secret_token,
)
from src.utils.logger import logger

router = APIRouter(tags=["webhooks"])

#: The header Telegram echoes back the registered secret in. Named once so the
#: route and its tests cannot drift apart on the spelling.
SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"


@dataclass(frozen=True)
class IngressRuntime:
    """What the route needs to execute a delivery. Wired at the composition root.

    `connect` matches the target tier's existing seam shape — `worker.py`'s
    ``connect=lambda: engine.connect()`` — rather than introducing a second
    convention for the same thing. Keep whatever is closed over
    process-lifetime, never request- or job-scoped: this object lives as long
    as the app does.

    `dispatch` is #854's slot. It receives the open connection and the admitted
    payload, and owns resolution and the command flips. Nothing about its
    contract beyond that is decided here.
    """

    connect: Callable[[], Any]
    dispatch: Callable[[Any, dict[str, Any]], Awaitable[Any]]
    #: Send *text* to a chat id — the acknowledgement a HANDLED `/start` gets
    #: after its delivery is committed (#1224 follow-up). None means the door
    #: stays silent by construction, which is what a deployment without the
    #: bot token gets. Best-effort: a failure here is logged, never surfaced.
    reply: Optional[Callable[[str, str], Awaitable[Any]]] = None


@router.post("/telegram")
# No per-IP ceiling on this route. The in-memory SlowAPI limiter that once
# declared 120/minute here died with the legacy app (#1028): the secret token
# is the real authentication control, and the number it carried was never
# validated against measured delivery rates (the route is still dormant). If
# the M.2 rehearsal shows a ceiling is wanted, it is a durable `rate_counters`
# scope, sized from those counts -- not a process-local bucket re-added here.
async def telegram_webhook(request: Request) -> dict[str, str]:
    """Admit one Telegram delivery, exactly once, and dispatch it.

    Refusals are ordered cheapest-first, and the last of them is the seam: a
    delivery arriving with no dispatcher wired is refused **without being
    admitted**, so the provider retries it rather than losing it.
    """
    if not verify_secret_token(
        request.headers.get(SECRET_HEADER),
        settings.TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN,
    ):
        # Deliberately not distinguishing "no secret configured" from "wrong
        # secret" in the response: the caller would learn whether the deployment
        # is armed, which is exactly what an unauthenticated prober wants.
        logger.warning("telegram webhook: secret token rejected")
        raise HTTPException(status_code=403, detail="forbidden")

    try:
        payload = await request.json()
    except Exception:
        logger.warning("telegram webhook: body is not JSON")
        raise HTTPException(status_code=400, detail="malformed body")

    if not isinstance(payload, dict):
        logger.warning("telegram webhook: body is not a JSON object")
        raise HTTPException(status_code=400, detail="malformed body")

    update_id = payload.get("update_id")
    if not isinstance(update_id, int):
        # Telegram sends `update_id` on every update. Absent or non-integer
        # means this is not a Telegram update, so there is no idempotency key
        # to admit under, and admitting under a synthesized one would be a lie.
        logger.warning("telegram webhook: no integer update_id")
        raise HTTPException(status_code=400, detail="missing update_id")

    runtime: Optional[IngressRuntime] = getattr(request.app.state, "ingress", None)
    if runtime is None:
        # THE SEAM. Refused before admission, on purpose. 503 rather than 200
        # so the delivery is not consumed: see the ordering rule in the module
        # docstring. The composition root wires `app.state.ingress` whenever an
        # engine exists; this branch is what a deployment without one answers.
        logger.warning(
            "telegram webhook PARKED: no dispatcher wired, delivery NOT admitted "
            "(update_id=%s). The provider will redeliver; nothing is lost. "
            "This clears when the composition root wires app.state.ingress.",
            update_id,
        )
        raise HTTPException(status_code=503, detail="ingress not wired")

    async with runtime.connect() as conn:
        try:
            await admit(
                conn,
                channel="telegram",
                external_ref=str(update_id),
                payload=payload,
                principal=TELEGRAM_PRINCIPAL,
            )
        except DeliveryReplayed:
            # Acknowledged WITHOUT re-execution — the two obligations L.8 names.
            logger.info("telegram webhook: replay of update_id=%s", update_id)
            return {"status": "replayed"}
        except AdmissionConflict:
            # Never swallowed as a replay: same key, different content.
            logger.warning(
                "telegram webhook: admission conflict on update_id=%s", update_id
            )
            raise HTTPException(status_code=409, detail="admission conflict")

        result = await runtime.dispatch(conn, payload)
        await conn.commit()

    # AFTER the commit and outside the connection: the link is durable before
    # any provider is spoken to, so a Telegram hiccup can neither roll it back
    # nor make Telegram redeliver (the 200 below stands regardless).
    await _acknowledge(runtime, payload, result)
    return {"status": "admitted"}


async def _acknowledge(runtime: IngressRuntime, payload: dict, result: Any) -> None:
    """Answer a HANDLED `/start` in the chat that tapped it. Refusals carry no
    reply by construction (`StartResult` enforces it), so a prober still learns
    nothing; a dispatch that returned nothing at all is left silent."""
    if runtime.reply is None:
        return
    if not getattr(result, "handled", False) or not getattr(result, "reply", None):
        return
    chat_id = ((payload.get("message") or {}).get("chat") or {}).get("id")
    if chat_id is None:
        return
    try:
        await runtime.reply(str(chat_id), result.reply)
    except Exception:  # noqa: BLE001 — best-effort, and the delivery is already committed
        logger.warning(
            "telegram webhook: acknowledgement not delivered (update_id=%s, outcome=%s)",
            payload.get("update_id"),
            getattr(result, "outcome", "?"),
        )
