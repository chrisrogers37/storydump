"""The Telegram webhook ingress route.

This route mounts `webhook_ingress`'s `verify_secret_token`, `fingerprint`
and `admit`. It stops at the resolution seam: tenant resolution belongs
to `tenant_resolution` and its resolver door (`fn_resolve_binding`, `fn_channel_join / fn_channel_leave`),
and none of it is here.

## Why admission needs no tenant

`admit()` takes `(conn, channel, external_ref, payload, principal)` and **no
tenant**. Admission keys on `(channel, principal, external_ref)`; resolution
happens afterwards, when something goes to *act* on the admitted command. The
idempotency boundary is upstream of the tenant boundary, so this half has no
dependency on the other.

## The ordering rule, which is the whole design

**A delivery that cannot be executed is refused BEFORE admission, never after.**

Admission is irreversible. The `command_dedup` key persists, so a later
redelivery of the same update is a `DeliveryReplayed` and is correctly *not*
executed. Admitting a delivery we cannot dispatch therefore does not defer the
command, it **destroys** it, and it destroys it wearing the shape of successful
deduplication. That is the failure `AdmissionConflict`'s docstring names as the
invisible one.

This is the faithful translation of the worker's parking discipline rather than
a departure from it. The worker parks an executor-less job by rescheduling it
**alive** and never finalizes it dead: the irreversible step there is
finalization, and the worker declines to take it. The irreversible step here
is admission, so this route declines to take it, and the provider's own
redelivery is the park cadence.

**The Telegram route keeps this ordering itself.** Web and CLI commands take the
same refuse-before-admit order through `commands.ingest` (`src/api/routes/v1.py`);
this route applies it to a Telegram delivery.

## Dormancy is enforced by configuration, not only by non-registration

`verify_secret_token` refuses when the expected value is absent, so a deployment
that has not set `TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN` refuses every delivery
at the door. Reaching admission requires setting the secret **and** registering
the webhook — two deliberate acts.

## Why `dispatch` receives the connection

Admission and effect are **one transaction**: `admit` and `dispatch`
run on one connection, and the route commits once. If the route committed
admission and then called a dispatcher on its own connection, a dispatch
failure would leave the key committed and the command unexecutable —
re-creating the loss this module exists to avoid, one layer down.
"""

from __future__ import annotations

import time

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.exc import TimeoutError as PoolTimeout


from src.config.settings import settings
from src.services.target import unit_of_work, vocabulary
from src.services.target.webhook_ingress import (
    AdmissionConflict,
    DeliveryReplayed,
    TELEGRAM_PRINCIPAL,
    admit,
    verify_secret_token,
)
from src.utils.logger import logger

router = APIRouter(tags=["webhooks"])

#: The header Telegram echoes back the registered secret in. The vocabulary
#: owns the spelling — the CLI's `webhook status` SENDS it and this route
#: READS it, so a second literal here is how `storydump webhook status`
#: starts reporting a refusal the live deliveries never see.
#: The route's tests import the name from here, as `v1.py` re-exports
#: `IDEMPOTENCY_HEADER = vocabulary.IDEMPOTENCY_HEADER`.
SECRET_HEADER = vocabulary.WEBHOOK_SECRET_HEADER


@dataclass(frozen=True)
class IngressRuntime:
    """What the route needs to execute a delivery. Wired at the composition root.

    `connect` matches the target tier's existing seam shape — `worker.py`'s
    ``connect=lambda: engine.connect()`` — rather than introducing a second
    convention for the same thing. Keep whatever is closed over
    process-lifetime, never request- or job-scoped: this object lives as long
    as the app does.

    `dispatch` is's slot. It receives the open connection and the admitted
    payload, and owns resolution and the command flips. Nothing about its
    contract beyond that is decided here.
    """

    connect: Callable[[], Any]
    dispatch: Callable[[Any, dict[str, Any]], Awaitable[Any]]
    #: Send *text* to a chat id — the acknowledgement a HANDLED `/start` gets
    #: after its delivery is committed. None means the door
    #: stays silent by construction, which is what a deployment without the
    #: bot token gets. Best-effort: a failure here is logged, never surfaced.
    reply: Optional[Callable[[str, str], Awaitable[Any]]] = None
    #: Answer a tap (`answerCallbackQuery`) — `(callback_query_id, text,
    #: show_alert) -> bool`. Best effort, after the commit. None = silent.
    answer_callback: Optional[Callable[[str, str, bool], Awaitable[bool]]] = None
    #: Edit a tapped message — `(chat_id, message_id, *, text=None,
    #: markup=None)`, the one seam. The identity link's
    #: confirmation prompt passes `text` (`TapResult.edit_text`: its buttons
    #: go); a card's Configure menu passes `markup` (`TapResult.edit_markup`).
    #: A card's outcome edit is the outbox's, paced. Best effort, after the
    #: commit. None = the message keeps its buttons; a stale prompt is refused
    #: by its spent state, and a stale menu heals on its next tap.
    edit: Optional[Callable[..., Awaitable[Any]]] = None


@dataclass
class TapMetrics:
    """Counters the operating details (`/api/v1/ops/health`) report: taps by
    outcome, and answers that did not land."""

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
# No per-IP ceiling on this route: the secret token is the real
# authentication control. If a ceiling is wanted, it is a durable
# `rate_counters` scope, sized from measured delivery rates -- not a
# process-local bucket here.
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
    # THE BOUNDARY: the pool's own wait is met at
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
        # No provider call inside the delivery's transaction.
        with unit_of_work.transaction_discipline():
            try:
                await admit(
                    conn,
                    channel="telegram",
                    external_ref=str(update_id),
                    payload=payload,
                    principal=TELEGRAM_PRINCIPAL,
                )
            except DeliveryReplayed:
                # Acknowledged WITHOUT re-execution.
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
        # rolls back with the connection (`TestAnAbortedWinnerDoesNotPoisonTheKey`),
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
    # out: the link is durable before any provider is spoken to, so a
    # Telegram hiccup can neither roll it back nor make Telegram redeliver —
    # and the request does not wait on Telegram's reply to the answer
    # before it returns (the task still runs to completion on this worker).
    background.add_task(_acknowledge, runtime, payload, result, metrics=metrics)
    outcome = getattr(result, "outcome", None)
    return (
        {"status": "admitted", "outcome": str(outcome)}
        if outcome is not None
        else {"status": "admitted"}
    )


#: What a tap hears when the ingress pool is saturated.
BUSY_TEXT = "Busy — tap again."


async def _answer_busy(
    runtime: IngressRuntime, callback_query_id: str, metrics: Optional[TapMetrics]
) -> None:
    """The busy toast, behind the 200: the transport answers False
    rather than raising; both are an answer that did not land, and the
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
    best effort; one answer per query. After a card command, the paced
    supersede edit sets the card's keyboard as it writes the outcome line in
    every binding, and the route edits nothing (one
    Telegram message per tap per binding — the bot's 30/s is the fleet's
    ceiling, and an unpaced strip would be a second message per tap that
    Telegram refuses past 30 simultaneous taps). Then comes the route's one
    edit (`IngressRuntime.edit`), when the result carries one: the identity
    link's prompt or an admitted waitlist ping (`edit_text`), or the keyboard
    a menu tap leaves (`edit_markup`). That is a second unpaced call, but a
    menu tap has spent the workspace's tap admission first
    (`telegram_dispatch._menu`), so one workspace's burst of them is capped
    (`TARGET_TAP_ADMISSION_PER_MINUTE`), and an admit edit follows only an
    operator's tap, whose button goes with it."""
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
    answer_ms = int((time.monotonic() - started) * 1000)
    edit_text = getattr(result, "edit_text", None)
    edit_markup = getattr(result, "edit_markup", None)
    if (
        (edit_text or edit_markup is not None)
        and runtime.edit is not None
        and result.chat_ref is not None
        and result.message_ref is not None
    ):
        try:
            await runtime.edit(
                result.chat_ref, result.message_ref, text=edit_text, markup=edit_markup
            )
        except Exception:  # noqa: BLE001 — best effort, and the delivery is committed
            logger.warning(
                "tap prompt not edited (update_id=%s, outcome=%s)",
                payload.get("update_id"),
                result.outcome,
            )
    logger.info(
        "tap answered update_id=%s outcome=%s answered=%s answer_ms=%d",
        payload.get("update_id"),
        result.outcome,
        answered,
        answer_ms,
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
    markup = getattr(result, "reply_markup", None)
    try:
        if markup:
            await runtime.reply(str(chat_id), result.reply, reply_markup=markup)
        else:
            await runtime.reply(str(chat_id), result.reply)
    except Exception:  # noqa: BLE001 — best-effort, and the delivery is already committed
        logger.warning(
            "telegram webhook: acknowledgement not delivered (update_id=%s, outcome=%s)",
            payload.get("update_id"),
            getattr(result, "outcome", "?"),
        )
