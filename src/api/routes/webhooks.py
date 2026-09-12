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

import time

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.exc import TimeoutError as PoolTimeout


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
    #: Answer a tap (`answerCallbackQuery`) — `(callback_query_id, text,
    #: show_alert) -> bool`. The tapped card's keyboard is removed by the
    #: paced supersede edit in the same call that writes the outcome line
    #: (2026-09-12: one Telegram message per tap per binding) — there is no
    #: separate unpaced strip any more.
    #: `(chat_ref, message_ref) -> bool`. Both best effort, after the commit
    #: (phase 1 of the 2026-09-09 tap plan, step 10). None = silent.
    answer_callback: Optional[Callable[[str, str, bool], Awaitable[bool]]] = None


@dataclass
class TapMetrics:
    """Counters `/health` reports: taps by outcome, answers and strips that
    did not land (phase 1 step 12)."""

    taps: dict[str, int] = field(default_factory=dict)
    answer_failed: int = 0

    def count(self, outcome: str) -> None:
        self.taps[outcome] = self.taps.get(outcome, 0) + 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "taps_total": sum(self.taps.values()),
            "taps": dict(sorted(self.taps.items())),
            "answer_failed": self.answer_failed,
        }


@router.post("/telegram")
# No per-IP ceiling on this route. The in-memory SlowAPI limiter that once
# declared 120/minute here died with the legacy app (#1028): the secret token
# is the real authentication control, and the number it carried was never
# validated against measured delivery rates (the route is still dormant). If
# the M.2 rehearsal shows a ceiling is wanted, it is a durable `rate_counters`
# scope, sized from those counts -- not a process-local bucket re-added here.
async def telegram_webhook(
    request: Request, background: BackgroundTasks
) -> dict[str, str]:
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

    logger.info(
        "telegram webhook: delivery update_id=%s kinds=%s",
        update_id,
        sorted(k for k in payload if k != "update_id"),
    )
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

    replayed = False
    result: Any = None
    metrics = getattr(request.app.state, "tap_metrics", None)
    # THE BOUNDARY (phase 2 step 2, F1 (a)+(d)): the pool's own wait is met at
    # the CHECKOUT and nowhere else, so "not admitted" is structural — no
    # transaction exists yet when it fires. A tap is answered by name without
    # a database so its spinner never outlives the budget, and its buttons
    # remain for a fresh tap; a message has no spinner, so it is refused and
    # Telegram redelivers.
    connection = runtime.connect()
    try:
        conn = await connection.__aenter__()
    except PoolTimeout:
        return _refuse_saturated(runtime, payload, metrics, background)
    try:
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
            replayed = True
        except AdmissionConflict:
            # Never swallowed as a replay: same key, different content.
            logger.warning(
                "telegram webhook: admission conflict on update_id=%s", update_id
            )
            raise HTTPException(status_code=409, detail="admission conflict")

        if not replayed:
            result = await runtime.dispatch(conn, payload)
            await conn.commit()
    except SQLAlchemyError as exc:
        # A database fault around admit()/dispatch/commit: the admission row
        # rolls back with the connection (L.8 `TestAnAbortedWinnerDoesNotPoisonTheKey`),
        # so the update is redelivered once the fault clears. Only THIS maps
        # to 503 after the boundary; a tap's own failures are named outcomes.
        logger.error(
            "telegram webhook: database fault on update_id=%s (%s) — 503, not admitted",
            update_id,
            type(exc).__name__,
        )
        raise HTTPException(status_code=503, detail="database unavailable")
    finally:
        await connection.__aexit__(None, None, None)

    if replayed:
        # OUTSIDE the connection and AFTER the 200: no pool slot and no
        # delivery slot is held across a provider call.
        background.add_task(_toast_replayed_tap, runtime, payload)
        return {"status": "replayed"}

    # AFTER the commit, outside the connection, and after the 200 has gone
    # out (#1284): the link is durable before any provider is spoken to, so a
    # Telegram hiccup can neither roll it back nor make Telegram redeliver —
    # and the request no longer waits on Telegram's reply to the answer
    # before it returns (the task still runs to completion on this worker).
    background.add_task(_acknowledge, runtime, payload, result, metrics=metrics)
    outcome = getattr(result, "outcome", None)
    return (
        {"status": "admitted", "outcome": str(outcome)}
        if outcome is not None
        else {"status": "admitted"}
    )


#: What a tap hears when the ingress pool is saturated (F1 (d)).
BUSY_TEXT = "Busy — tap again."


async def _answer_busy(
    runtime: IngressRuntime, callback_query_id: str, metrics: Optional[TapMetrics]
) -> None:
    """The busy toast, behind the 200 (#1284): the transport answers False
    rather than raising; both are an answer that did not land, and the F1
    bound is read from `answer_failed` either way."""
    try:
        landed = await runtime.answer_callback(callback_query_id, BUSY_TEXT, False)
    except Exception:  # noqa: BLE001 — best effort, no database
        landed = False
    if landed is False and metrics is not None:
        metrics.answer_failed += 1


def _refuse_saturated(
    runtime: IngressRuntime,
    payload: dict,
    metrics: Optional[TapMetrics],
    background: BackgroundTasks,
) -> dict[str, str]:
    """The pool wait ran out before admission. EVERY `callback_query` with an
    id is answered "Busy — tap again" (best effort, no database, behind the
    200 like every other answer — the saturated path is the one where a
    request waiting on Telegram is dearest) and the delivery is consumed
    with 200 `refused/busy` — the tap is re-derivable because its buttons
    remain; a stale or malformed token is still a real spinner, and busy is
    the one thing true of it here (unsaturated it would hear `older_card`).
    Anything else is refused 503 before admission so the provider redelivers
    (a message has no spinner to protect)."""
    cq = payload.get("callback_query")
    if isinstance(cq, dict) and cq.get("id") is not None:
        if metrics is not None:
            metrics.count("busy")
        logger.warning(
            "telegram webhook: pool saturated — tap update_id=%s answered busy,"
            " not admitted",
            payload.get("update_id"),
        )
        if runtime.answer_callback is not None:
            background.add_task(_answer_busy, runtime, str(cq["id"]), metrics)
        return {"status": "refused", "outcome": "busy"}
    logger.warning(
        "telegram webhook PARKED: pool saturated, delivery NOT admitted"
        " (update_id=%s); the provider will redeliver",
        payload.get("update_id"),
    )
    raise HTTPException(status_code=503, detail="ingress saturated")


async def _toast_replayed_tap(runtime: IngressRuntime, payload: dict) -> None:
    """A redelivered tap (Telegram retried while the first delivery's answer
    was on its way) is a fresh query whose spinner is still turning: say so,
    best effort, without touching the database."""
    cq = payload.get("callback_query")
    if (
        not isinstance(cq, dict)
        or cq.get("id") is None
        or runtime.answer_callback is None
    ):
        return
    try:
        await runtime.answer_callback(str(cq["id"]), "Got it — already handled.", False)
    except Exception:  # noqa: BLE001 — best effort
        logger.warning(
            "telegram webhook: replayed tap not answered (update_id=%s)",
            payload.get("update_id"),
        )


async def _answer_tap(
    runtime: IngressRuntime, payload: dict, result: Any, metrics: Optional[TapMetrics]
) -> None:
    """After the commit: the tap's answer (its toast or alert), unpaced and
    best effort. The card's keyboard goes with the paced supersede edit that
    writes the outcome line in every binding (F4 (a) as built 2026-09-12: one
    Telegram message per tap per binding — the bot's 30/s is the fleet's
    ceiling, and an unpaced strip was a second message per tap that Telegram
    refused past 30 simultaneous taps). One answer per query."""
    started = time.monotonic()
    if metrics is not None:
        metrics.count(result.outcome)
    answered = None
    if runtime.answer_callback is not None and result.callback_query_id:
        try:
            answered = await runtime.answer_callback(
                result.callback_query_id, result.answer_text, result.show_alert
            )
        except Exception:  # noqa: BLE001 — best effort, and the delivery is committed
            answered = False
        if answered is False and metrics is not None:
            metrics.answer_failed += 1
    logger.info(
        "tap answered update_id=%s outcome=%s answered=%s answer_ms=%d",
        payload.get("update_id"),
        result.outcome,
        answered,
        int((time.monotonic() - started) * 1000),
    )


async def _acknowledge(
    runtime: IngressRuntime,
    payload: dict,
    result: Any,
    *,
    metrics: Optional[TapMetrics] = None,
) -> None:
    """Answer a HANDLED `/start` in the chat that tapped it, or a tap's
    callback query. Refusals of a `/start` carry no reply by construction
    (`StartResult` enforces it), so a prober still learns nothing; a dispatch
    that returned nothing at all is left silent."""
    if hasattr(result, "answer_text"):  # a TapResult — counted even unanswerable
        await _answer_tap(runtime, payload, result, metrics)
        return
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
