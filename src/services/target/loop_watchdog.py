"""The worker's event-loop watchdog: the actor for a stall nothing on the loop can see.

The worker root is fail-fast (`src/worker.py`): `supervise()` turns a supervised
task's DEATH into `WorkerTaskDied` and a non-zero exit, and `worker_health`
reports a clock that has stopped advancing. Both run ON the event loop, so
neither can act in the two cases where the loop itself is not turning:

* **a call that does not return** on the loop's thread. Every task, the status
  line, the health listener and the signal handlers stop together, and nothing
  is logged, because the code that would log is the code that is not running;
* **a process that lost time**: stopped and resumed (a frozen container, a
  debugger). It wakes holding connections the database closed long ago and
  leases the reaper already returned. Most of that surfaces as errors and a
  task death within seconds; exiting at once is the same end without the
  steps in between.

The watchdog is therefore a THREAD, the one place in the process that can run
while the loop does not. Its whole action is to say why and end the process,
which the platform restarts when the exit is non-zero. Ending it is safe by
the design the rest of the worker already relies on: a killed worker loses
nothing (session-scoped election, lease-token fencing, permits committed
before each provider call).

## What it cannot see

* **A loop that turns while its tasks wait**: a database that is down, a
  provider that is slow, a reply that was lost. From here a wait that will
  never end cannot be told from an outage, and exiting through an outage
  spends `restartPolicyMaxRetries` on something no restart mends
  (`worker_health`'s header draws the same line for its own gate). So this
  thread never acts on a wait; a wait is to be bounded where it is made.
* **A call that holds the interpreter lock** for the whole stall. This thread
  needs that lock to run, so it sees such a call only once it returns, as lost
  time, and never if it does not return.
* **A process that never runs again.** A host that is gone takes this thread
  with it. Only something outside the process can heal that.
"""

from __future__ import annotations

import faulthandler
import os
import sys
import threading
import time
from typing import Callable, Optional

#: What the process exits with when the watchdog ends it. Non-zero, so the
#: platform's ON_FAILURE policy restarts the worker; and neither 1 (a supervised
#: task died, `WorkerTaskDied`) nor 2 (a refusal to boot), so the deploy log
#: names this cause alone.
STALL_EXIT_CODE = 3

#: How long the exit waits for its own report before it exits regardless. The
#: report goes to the same stream the stuck thread may be blocked on, and a
#: watchdog that waited for it would be one more thing that never returns.
REPORT_GRACE_SECONDS = 5.0


def exit_stalled(
    reason: str, *, stream=None, grace_seconds: float = REPORT_GRACE_SECONDS
) -> None:
    """Say why, dump every thread's stack, and end the process non-zero.

    The stacks are the point of saying anything: a stall leaves no log line of
    its own, and the stuck thread's frame is the one fact that names the call.
    Written to *stream* directly (default: stderr) and never through `logging`,
    whose handler lock the stuck thread may hold.

    `os._exit`, not `sys.exit`: `SystemExit` raised in a thread ends that
    thread only, and the loop that would run the cleanup is the thing that is
    stuck. The exit is in a `finally` because the report is optional and the
    exit is not: a process too sick to start one more thread must still leave.
    """
    out = stream if stream is not None else sys.stderr

    def report() -> None:
        try:
            out.write(
                f"FATAL: worker watchdog: {reason}. Every thread's stack follows;"
                f" exiting {STALL_EXIT_CODE} so the platform restarts the worker.\n"
            )
            out.flush()
            faulthandler.dump_traceback(file=out, all_threads=True)
            out.flush()
        except Exception:  # noqa: BLE001 — a report that fails must not stop the exit
            pass

    try:
        reporter = threading.Thread(
            target=report, name="loop-watchdog-report", daemon=True
        )
        reporter.start()
        reporter.join(grace_seconds)
    finally:
        os._exit(STALL_EXIT_CODE)


class LoopWatchdog:
    """Ends the process when its event loop stops turning. Single use: `start`, then `stop`.

    Two verdicts, read by :meth:`check` on the watchdog's own thread:

    * the loop has not stamped a beat for more than `stall_seconds`;
    * the thread itself was absent for more than a stall and a beat between
      two of its own checks, so the whole process lost that time (or one call
      held the interpreter lock for it). Decided on the thread's own timeline,
      so it does not depend on which thread woke first.

    `armed` says the thread is alive, for the status line: it is outside
    `supervise()`, so the line is where its death would show. `beats` counts
    the loop's stamps.
    """

    def __init__(
        self,
        *,
        stall_seconds: float,
        beat_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        on_stall: Callable[[str], None] = exit_stalled,
    ):
        if not 0 < beat_seconds < stall_seconds:
            raise ValueError(
                f"beat_seconds must be above 0 and below stall_seconds"
                f" ({stall_seconds}), got {beat_seconds}"
            )
        self._stall_seconds = stall_seconds
        self._beat_seconds = beat_seconds
        self._clock = clock
        self._on_stall = on_stall
        self._last_beat = self._last_check = clock()
        self._stopping = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._timer = None
        self.beats = 0

    def beat(self) -> None:
        """The loop turned. Called on the loop's thread."""
        self._last_beat = self._clock()
        self.beats += 1

    def check(self) -> Optional[str]:
        """One reading: the reason the process must end, or None."""
        now = self._clock()
        away = now - self._last_check
        self._last_check = now
        if away > self._beat_seconds + self._stall_seconds:
            return (
                f"the watchdog was absent {away:.0f} s between two checks"
                f" {self._beat_seconds:.0f} s apart: the process was stopped and"
                f" resumed, or one call held the interpreter lock that long"
            )
        quiet = now - self._last_beat
        if quiet > self._stall_seconds:
            return (
                f"the event loop has not turned for {quiet:.0f} s"
                f" (bound {self._stall_seconds:.0f} s)"
            )
        return None

    @property
    def armed(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, loop) -> None:
        """Arm both halves, on the loop's own thread: the checks on a daemon
        thread, then the beat on *loop*, which re-arms itself every
        `beat_seconds`. The thread first, so a process that cannot start one
        raises here with nothing left armed."""
        self._last_check = self._clock()
        thread = threading.Thread(target=self._watch, name="loop-watchdog", daemon=True)
        thread.start()
        self._thread = thread

        def turn() -> None:
            self.beat()
            self._timer = loop.call_later(self._beat_seconds, turn)

        turn()

    def _watch(self) -> None:
        while not self._stopping.wait(self._beat_seconds):
            reason = self.check()
            if reason is not None:
                self._on_stall(reason)
                return

    def stop(self) -> None:
        """Disarm both halves, on the loop's thread. Harmless before `start`
        and when repeated."""
        self._stopping.set()
        if self._timer is not None:
            self._timer.cancel()
        if self._thread is not None:
            self._thread.join(self._beat_seconds + REPORT_GRACE_SECONDS)
