"""The worker's event-loop watchdog (#1664).

`supervise()` ends the worker when a task dies, and the health endpoint reports
a clock that stopped advancing. Both run ON the event loop, so neither can act
when the loop itself stops turning. These tests pin the one actor that can: a
thread that ends the process, so the platform restarts it.

The load-bearing test is
`test_a_blocked_event_loop_is_seen_from_the_thread_while_it_is_still_blocked`:
it blocks a real loop with a synchronous call and asserts the verdict arrives
BEFORE the call returns, which nothing running on that loop could do.

The verdict tests move a clock by hand, so no bound is ever raced against the
machine. The thread tests use real time only as a LOWER bound (a stall is
waited for, never timed out on), so a slow or starved runner makes them
slower, not red.
"""

import asyncio
import threading
import time

import pytest

from src.services.target import loop_watchdog
from src.services.target.loop_watchdog import LoopWatchdog


class _Clock:
    """A monotonic clock the test moves by hand."""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def _watchdog(clock, **numbers) -> LoopWatchdog:
    numbers.setdefault("stall_seconds", 120.0)
    numbers.setdefault("beat_seconds", 5.0)
    return LoopWatchdog(clock=clock, on_stall=lambda reason: None, **numbers)


async def _until(predicate, *, within: float = 10.0) -> None:
    deadline = time.monotonic() + within
    while not predicate():
        assert time.monotonic() < deadline, "the condition never came true"
        await asyncio.sleep(0.01)


class TestVerdict:
    def test_a_loop_that_keeps_beating_is_never_stalled(self):
        clock = _Clock()
        dog = _watchdog(clock)
        for _ in range(200):
            clock.now += 5.0
            dog.beat()
            assert dog.check() is None

    def test_a_silent_loop_is_stalled_past_the_bound_and_not_at_it(self):
        clock = _Clock()
        dog = _watchdog(clock)
        dog.beat()
        # The thread keeps checking on time; only the loop's beat has stopped.
        for _ in range(24):
            clock.now += 5.0
            assert dog.check() is None, "120 s is the bound, not past it"
        clock.now += 5.0
        reason = dog.check()
        assert reason is not None
        assert "event loop" in reason and "125" in reason

    def test_a_beat_inside_the_bound_clears_a_long_quiet_spell(self):
        clock = _Clock()
        dog = _watchdog(clock)
        for _ in range(23):
            clock.now += 5.0
            assert dog.check() is None
        dog.beat()
        for _ in range(24):
            clock.now += 5.0
            assert dog.check() is None

    def test_a_process_that_lost_time_is_stalled_even_when_the_loop_beat_first(self):
        # Stopped and resumed: both threads wake together. If the loop thread
        # runs first it stamps a fresh beat, and a verdict that read only the
        # beat would let a process that was absent for ten minutes carry on
        # with connections and leases that are long dead. The thread's own
        # timeline decides instead, so the verdict does not depend on the race.
        clock = _Clock()
        dog = _watchdog(clock)
        dog.beat()
        assert dog.check() is None
        clock.now += 600.0
        dog.beat()
        reason = dog.check()
        assert reason is not None
        assert "lost" in reason and "600" in reason

    def test_a_late_check_inside_the_bound_is_not_lost_time(self):
        # A starved thread is late, not absent: one check a minute late must
        # not end a worker whose loop is turning.
        clock = _Clock()
        dog = _watchdog(clock)
        clock.now += 60.0
        dog.beat()
        assert dog.check() is None

    @pytest.mark.parametrize("beat", [0.0, -1.0, 120.0, 121.0])
    def test_the_bound_must_be_longer_than_a_beat(self, beat):
        with pytest.raises(ValueError, match="beat_seconds"):
            LoopWatchdog(stall_seconds=120.0, beat_seconds=beat)


class TestTheThread:
    def test_a_blocked_event_loop_is_seen_from_the_thread_while_it_is_still_blocked(
        self,
    ):
        """THE behaviour. A synchronous call holds the loop; the loop's own
        beat cannot run, and neither could any task on it. The verdict has to
        arrive while the call is still blocking."""
        fired = threading.Event()
        reasons = []

        def on_stall(reason):
            reasons.append(reason)
            fired.set()

        dog = LoopWatchdog(stall_seconds=0.5, beat_seconds=0.02, on_stall=on_stall)
        seen = []

        async def scenario():
            dog.start(asyncio.get_running_loop())
            await _until(lambda: dog.beats >= 3)  # the loop is turning
            seen.append(fired.is_set())
            # The synchronous wait. It returns when the watchdog fires; a
            # watchdog that needed the loop would leave it to time out.
            seen.append(fired.wait(30.0))

        try:
            asyncio.run(scenario())
        finally:
            dog.stop()
        assert seen == [False, True]
        assert len(reasons) == 1 and "event loop" in reasons[0]

    async def test_the_loop_beats_on_its_own_until_stop(self):
        # No bound is in play (an hour): this pins the loop's half alone.
        dog = LoopWatchdog(stall_seconds=3600.0, beat_seconds=0.01)
        dog.start(asyncio.get_running_loop())
        try:
            await _until(lambda: dog.beats >= 3)
            assert dog.alive
        finally:
            dog.stop()
        assert not dog.alive, "stop() must end the thread, or it outlives the worker"
        settled = dog.beats
        await asyncio.sleep(0.1)
        assert dog.beats == settled, "stop() must cancel the loop's beat too"

    def test_stop_before_start_and_twice_is_harmless(self):
        loop = asyncio.new_event_loop()
        try:
            dog = LoopWatchdog(stall_seconds=3600.0, beat_seconds=0.01)
            dog.stop()
            dog.start(loop)
            dog.stop()
            dog.stop()
            assert not dog.alive
        finally:
            loop.close()

    def test_it_fires_once_and_then_the_thread_ends(self):
        # In production the action never returns (the process is gone). Under
        # a test it does, and a thread that fired again at every check would
        # report one stall many times. The loop here is never run, which is a
        # loop that does not turn.
        reasons = []
        loop = asyncio.new_event_loop()
        try:
            dog = LoopWatchdog(
                stall_seconds=0.2, beat_seconds=0.02, on_stall=reasons.append
            )
            dog.start(loop)
            deadline = time.monotonic() + 30.0
            while dog.alive and time.monotonic() < deadline:
                time.sleep(0.01)
            assert not dog.alive
            dog.stop()
        finally:
            loop.close()
        assert len(reasons) == 1


class TestTheExit:
    def test_it_names_the_reason_dumps_every_thread_and_exits_non_zero(
        self, monkeypatch, tmp_path
    ):
        codes = []
        monkeypatch.setattr(loop_watchdog.os, "_exit", codes.append)
        with open(tmp_path / "stderr.txt", "w+") as stream:
            loop_watchdog.exit_stalled("the event loop has not turned", stream=stream)
            stream.seek(0)
            said = stream.read()
        assert codes == [loop_watchdog.STALL_EXIT_CODE]
        assert "the event loop has not turned" in said
        # The stack of the thread that is stuck is the diagnosis a silent
        # stall never leaves behind: faulthandler writes one per thread.
        assert "most recent call first" in said

    def test_the_exit_code_restarts_the_worker_and_names_this_cause_alone(self):
        # 0 is a clean stop, which ON_FAILURE does not restart; 1 is a task
        # death (`WorkerTaskDied`) and 2 a refusal to boot.
        assert loop_watchdog.STALL_EXIT_CODE not in (0, 1, 2)

    def test_a_blocked_stream_cannot_keep_a_stalled_worker_alive(self, monkeypatch):
        # The order is the assertion: an exit that waited for the stream would
        # still exit once the write gave up, and a test that only counted the
        # exit would pass on it.
        order = []
        monkeypatch.setattr(
            loop_watchdog.os, "_exit", lambda code: order.append(("exit", code))
        )
        release = threading.Event()

        class _Blocked:
            def write(self, text):
                release.wait(30.0)
                order.append("the stream took the write")

            def flush(self):
                pass

            def fileno(self):
                raise OSError("no descriptor")

        try:
            loop_watchdog.exit_stalled(
                "the event loop has not turned", stream=_Blocked(), grace_seconds=0.1
            )
            assert order == [("exit", loop_watchdog.STALL_EXIT_CODE)]
        finally:
            release.set()
