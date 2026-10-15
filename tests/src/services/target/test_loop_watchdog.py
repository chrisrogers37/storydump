"""The worker's event-loop watchdog (#1664): `loop_watchdog`.

The verdict tests move a clock by hand, so no bound is ever raced against the
machine. The thread tests use real time as a LOWER bound only (a stall is
waited for, never timed out on), so a slow or starved runner makes them
slower, not red. The one upper bound is in the blocked-loop test, and its
margin is two seconds.
"""

import asyncio
import threading
import time

import pytest

from src.services.target import loop_watchdog
from src.services.target.loop_watchdog import LoopWatchdog


class _Clock:
    """A monotonic clock the test moves by hand."""

    now = 1000.0

    def __call__(self) -> float:
        return self.now


async def _until(predicate) -> None:
    deadline = time.monotonic() + 10.0
    while not predicate():
        assert time.monotonic() < deadline, "the condition never came true"
        await asyncio.sleep(0.01)


def _refuse_new_threads(monkeypatch) -> None:
    def refuse(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(loop_watchdog.threading.Thread, "start", refuse)


class TestVerdict:
    def test_a_silent_loop_is_stalled_past_the_bound_and_not_at_it(self):
        clock = _Clock()
        dog = LoopWatchdog(stall_seconds=120.0, beat_seconds=5.0, clock=clock)
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
        dog = LoopWatchdog(stall_seconds=120.0, beat_seconds=5.0, clock=clock)
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
        # beat would let a process that was absent for ten minutes carry on.
        # The thread's own timeline decides, so the race does not.
        clock = _Clock()
        dog = LoopWatchdog(stall_seconds=120.0, beat_seconds=5.0, clock=clock)
        dog.beat()
        assert dog.check() is None
        clock.now += 600.0
        dog.beat()
        reason = dog.check()
        assert reason is not None
        assert "absent" in reason and "600" in reason

    def test_a_late_check_inside_the_bound_is_not_lost_time(self):
        # A starved thread is late, not absent: one check a minute late must
        # not end a worker whose loop is turning.
        clock = _Clock()
        dog = LoopWatchdog(stall_seconds=120.0, beat_seconds=5.0, clock=clock)
        clock.now += 60.0
        dog.beat()
        assert dog.check() is None

    @pytest.mark.parametrize("beat", [0.0, 120.0])
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

        dog = LoopWatchdog(stall_seconds=2.0, beat_seconds=0.05, on_stall=on_stall)

        async def scenario():
            dog.start(asyncio.get_running_loop())
            await _until(lambda: dog.beats >= 3)  # the loop is turning
            before = fired.is_set()
            # The synchronous wait. It returns when the watchdog fires; a
            # watchdog that needed the loop would leave it to time out.
            during = fired.wait(30.0)
            return before, during

        try:
            assert asyncio.run(scenario()) == (False, True)
        finally:
            dog.stop()
        assert "event loop" in reasons[0]

    async def test_the_loop_beats_on_its_own_until_stop(self):
        # No bound is in play (an hour): this pins the loop's half alone.
        dog = LoopWatchdog(stall_seconds=3600.0, beat_seconds=0.01)
        dog.start(asyncio.get_running_loop())
        try:
            await _until(lambda: dog.beats >= 3)
            assert dog.armed
        finally:
            dog.stop()
        assert not dog.armed, "stop() must end the thread, or it outlives the worker"
        settled = dog.beats
        await asyncio.sleep(0.1)
        assert dog.beats == settled, "stop() must cancel the loop's beat too"
        dog.stop()  # and again: harmless

    def test_it_fires_once_and_then_the_thread_ends(self):
        # In production the action never returns (the process is gone). Under
        # a test it does, and a thread that fired again at every check would
        # report one stall many times. The loop here is never run, which is a
        # loop that does not turn.
        reasons = []
        loop = asyncio.new_event_loop()
        try:
            dog = LoopWatchdog(
                stall_seconds=0.05, beat_seconds=0.01, on_stall=reasons.append
            )
            dog.start(loop)
            deadline = time.monotonic() + 30.0
            while dog.armed and time.monotonic() < deadline:
                time.sleep(0.01)
            assert not dog.armed
            dog.stop()
        finally:
            loop.close()
        assert len(reasons) == 1

    def test_a_process_that_cannot_start_a_thread_is_left_with_nothing_armed(
        self, monkeypatch
    ):
        # The sick-process case. A half-armed watchdog would beat for nobody,
        # and a stop() that tripped on the thread it never started would raise
        # in whatever cleanup called it.
        _refuse_new_threads(monkeypatch)
        loop = asyncio.new_event_loop()
        try:
            dog = LoopWatchdog(stall_seconds=3600.0, beat_seconds=0.01)
            with pytest.raises(RuntimeError, match="can't start new thread"):
                dog.start(loop)
            assert dog.beats == 0 and not dog.armed
            dog.stop()
        finally:
            loop.close()


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

        try:
            loop_watchdog.exit_stalled(
                "the event loop has not turned", stream=_Blocked(), grace_seconds=0.01
            )
            assert order == [("exit", loop_watchdog.STALL_EXIT_CODE)]
        finally:
            release.set()

    def test_a_report_that_cannot_start_does_not_stop_the_exit(self, monkeypatch):
        codes = []
        monkeypatch.setattr(loop_watchdog.os, "_exit", codes.append)
        _refuse_new_threads(monkeypatch)
        with pytest.raises(RuntimeError, match="can't start new thread"):
            loop_watchdog.exit_stalled("the event loop has not turned")
        assert codes == [loop_watchdog.STALL_EXIT_CODE]
