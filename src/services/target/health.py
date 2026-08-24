"""The target composition root's ``/health`` endpoint (#942 parity gap).

`railway.toml` sets ``healthcheckPath = "/health"`` for both services. The
LEGACY root serves it from `src/main.py`; the target root shipped without it,
so Railway probed a port nothing listened on, timed out, and marked three
consecutive deploys FAILED while the worker itself was healthy — keeping an
older, pre-cutover build serving.

`src/main.py` already carries the lesson from the previous occurrence of this
exact trap: start the listener BEFORE the slow steps, because Railway is timing
the socket, not the program. This module is that rule applied to the other root.

## What 200 means here, which is NOT what it means for legacy

Legacy reports per-loop staleness against `LOOP_EXPECTED_INTERVALS` and 503s on
a stale loop, because its loops can go quiet while the process lives.

The target root is fail-fast instead: `supervise()` turns ANY supervised task
death into `WorkerTaskDied`, and `main()` turns that into `SystemExit(1)`. A
dead task therefore takes the process with it, and the socket stops answering —
a stronger signal than a 503, and one this endpoint could not improve on.

So the one failure this endpoint can see that the supervisor cannot is a worker
that is ALIVE but STUCK: a task blocked forever inside its loop has not exited,
so `supervise` never fires, and the counters simply stop moving. That is the
condition a restart actually repairs, and it is the only one that 503s here.

**Deliberately NOT a threshold on `consecutive_failures`.** Those counters are
documented as "what a liveness check reads" (`scheduler.Clock`,
`outbox.OutboxPoller`), and they are reported in the body — but a database blip
would raise them on a worker that is going to recover, and 503-ing would spend
`restartPolicyMaxRetries` restarting through an outage no restart can fix. They
are diagnostic here, not a gate.

**No startup grace constant, and that is derived rather than copied.** Legacy
needs one because it reports tick-based staleness that reads stale before the
first tick. This endpoint's staleness clock starts at the first observation of a
STARTED clock, so a booting worker is healthy by construction — there is no
window to paper over. The listener also binds before the first database
connection, so it answers during startup regardless of how slow that is.
"""

from __future__ import annotations

import asyncio
import json
import os
from time import monotonic

from src.utils.logger import logger

#: Multiple of the clock interval after which a non-advancing clock is stale.
#: Legacy uses the same 2x-expected-interval rule in
#: `services/core/loops/heartbeat.py`; this is that precedent, not a new number.
STALE_INTERVAL_MULTIPLE = 2.0


class HealthState:
    """Tracks whether the clock is still advancing, sampled on each probe.

    Sampling on the probe rather than from a background task is deliberate: a
    supervised sampler would be one more task that can die, and its death would
    be reported by the very mechanism this endpoint exists to complement.
    """

    def __init__(self, app) -> None:
        self._app = app
        self._last_ticks: int | None = None
        self._last_change: float | None = None

    def _clock_stalled_for(self) -> float | None:
        """Seconds the clock has been stuck, or None if it is fine/unstarted."""
        clock = getattr(self._app, "clock", None)
        if clock is None:
            # Still booting — connections and the election have not happened.
            # Not stale; there is nothing yet that could be.
            return None
        now = monotonic()
        ticks = getattr(clock, "ticks", 0)
        if self._last_ticks is None or ticks != self._last_ticks:
            self._last_ticks = ticks
            self._last_change = now
            return None
        interval = getattr(self._app.config, "clock_interval_seconds", 60.0)
        stalled = now - (self._last_change or now)
        return stalled if stalled > interval * STALE_INTERVAL_MULTIPLE else None

    def observables(self) -> dict:
        """The counters, reported whatever the verdict — diagnosis, not a gate."""
        app = self._app
        clock = getattr(app, "clock", None)
        hb = getattr(app, "heartbeat", None)
        return {
            "lanes": {
                wl.lane: {
                    "processed": wl.processed,
                    "parked": wl.parked,
                    "failures": wl.failures,
                    "fenced": wl.fenced,
                }
                for wl in getattr(app, "loops", []) or []
            },
            "clock": (
                {
                    "elected": clock.elected,
                    "ticks": clock.ticks,
                    "inserts": clock.inserts,
                    "consecutive_failures": clock.consecutive_failures,
                }
                if clock is not None
                else "unstarted"
            ),
            "heartbeat": (
                {
                    "beats": hb.beats,
                    "short_beats": hb.short_beats,
                    "consecutive_failures": hb.consecutive_failures,
                }
                if hb is not None
                else "unstarted"
            ),
        }

    def response(self) -> bytes:
        stalled = self._clock_stalled_for()
        payload: dict = {"observables": self.observables()}
        if stalled is None:
            payload["status"] = "healthy"
            status_line = "HTTP/1.1 200 OK"
        else:
            payload["status"] = "unhealthy"
            payload["stalled_seconds"] = round(stalled, 1)
            payload["reason"] = "clock has not advanced; the worker is alive but stuck"
            status_line = "HTTP/1.1 503 Service Unavailable"
        body = json.dumps(payload).encode()
        return (
            f"{status_line}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode() + body


async def serve_health(app) -> asyncio.AbstractServer:
    """Bind the health listener. Call BEFORE any slow startup step.

    Returns the server so the caller owns its lifetime; binding is awaited so a
    port conflict surfaces at boot rather than as a silent non-listener.
    """
    state = HealthState(app)
    port = int(os.environ.get("PORT", 8080))

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            await reader.readline()  # consume the request line; path is ignored
            writer.write(state.response())
            await writer.drain()
        except Exception as exc:  # noqa: BLE001 — a probe must never kill the worker
            logger.warning("health probe failed: %s", exc)
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "0.0.0.0", port)
    logger.info("health endpoint listening on :%s", port)
    return server
