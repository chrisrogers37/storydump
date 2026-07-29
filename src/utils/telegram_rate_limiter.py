"""Telegram rate limiter with a multi-user smoke alarm (#686b).

Wraps PTB's ``AIORateLimiter`` (flood smoothing) and adds one observability
signal: a throttled warning when the **overall** (cross-tenant) rate bucket —
not the per-group bucket — is the binding constraint.

Per-group throttling is expected and healthy: each chat is capped at Telegram's
per-group limit and bursts are simply spread out. The **overall** 30/s cap
saturating is different — it means aggregate load *across all tenants* is
nearing the single-bot-token ceiling (the Track-C horizontal-scale limit that
per-tenant tokens or a webhook fan-in would raise). That is the signal worth an
alert, so it gets its own log marker.
"""

import time
from collections.abc import Callable, Coroutine
from typing import Any

from telegram.ext import AIORateLimiter

from src.utils.logger import logger

# The overall-saturation alarm fires at most once per window so a sustained
# burst doesn't flood the log with its own alarm.
_OVERALL_ALARM_THROTTLE_S = 60.0

# Greppable marker for log-based alerting on the multi-user ceiling.
OVERALL_SATURATION_MARKER = "telegram-overall-rate-saturated"


class SmokeAlarmRateLimiter(AIORateLimiter):
    """``AIORateLimiter`` that flags when the OVERALL bucket is the limiter.

    Behaviour is identical to the base limiter; the only addition is the
    throttled smoke-alarm log. The hook overrides the (internal) ``_run_request``
    seam — stable for our pinned ``python-telegram-bot==22.7``; if a future PTB
    renames it the alarm simply stops firing (the limiter itself keeps working).
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._last_overall_alarm: float = 0.0

    def _overall_bucket_saturated(self, chat: bool, allow_paid_broadcast: bool) -> bool:
        """True when the next call would block on the overall (not group) bucket.

        ``AsyncLimiter.has_capacity()`` needs a running loop — always satisfied
        here (``_run_request`` is only awaited inside the bot loop). Best-effort:
        any error is swallowed so observability can never break a send.
        """
        if allow_paid_broadcast or not chat or self._base_limiter is None:
            return False
        try:
            return not self._base_limiter.has_capacity()
        except Exception:  # noqa: BLE001 — observability must never break a send
            return False

    def _maybe_alarm(self) -> None:
        now = time.monotonic()
        if now - self._last_overall_alarm < _OVERALL_ALARM_THROTTLE_S:
            return
        self._last_overall_alarm = now
        logger.warning(
            "%s: overall Telegram rate bucket saturated (%s/%ss, cross-tenant) "
            "— multi-user smoke alarm: aggregate bot-token load is nearing the "
            "single-token ceiling (Track C — per-tenant tokens / webhook fan-in "
            "raise it). Per-group throttling alone would not trip this.",
            OVERALL_SATURATION_MARKER,
            self._base_limiter.max_rate,
            self._base_limiter.time_period,
        )

    async def _run_request(
        self,
        chat: bool,
        group: "str | int | bool",
        allow_paid_broadcast: bool,
        callback: Callable[..., Coroutine[Any, Any, Any]],
        args: Any,
        kwargs: "dict[str, Any]",
    ) -> Any:
        if self._overall_bucket_saturated(chat, allow_paid_broadcast):
            self._maybe_alarm()
        return await super()._run_request(
            chat=chat,
            group=group,
            allow_paid_broadcast=allow_paid_broadcast,
            callback=callback,
            args=args,
            kwargs=kwargs,
        )
