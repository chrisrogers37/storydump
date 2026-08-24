"""The target root's /health endpoint (#942 parity gap).

The defect these cover is not "the response is wrong" — it is that no listener
existed at all, so Railway timed out and marked three healthy deploys FAILED
while an older pre-cutover build kept serving. The load-bearing test is
`test_the_listener_answers_while_the_first_db_connection_is_still_blocking`:
it fails against the pre-fix tree because nothing binds the port.
"""

import asyncio
import json

import pytest

from src.services.target import health as health_endpoint


class _Loop:
    def __init__(self, lane):
        self.lane = lane
        self.processed, self.parked, self.failures, self.fenced = 3, 1, 0, 0


class _Clock:
    def __init__(self, ticks=0):
        self.ticks = ticks
        self.inserts, self.elected, self.consecutive_failures = 0, True, 0


class _Heartbeat:
    beats, short_beats, consecutive_failures = 12, 0, 0


class _Config:
    clock_interval_seconds = 10.0


class _App:
    def __init__(self, clock=None):
        self.loops = [_Loop("interactive"), _Loop("bulk")]
        self.clock = clock
        self.heartbeat = _Heartbeat()
        self.config = _Config()


def _parse(raw: bytes):
    head, _, body = raw.partition(b"\r\n\r\n")
    return head.decode().splitlines()[0], json.loads(body)


class TestVerdict:
    def test_booting_is_healthy_with_no_grace_constant(self):
        # clock is None: connections and the election have not happened yet.
        # Legacy needs a 120s grace because it reports tick staleness from the
        # first moment; this root has nothing stale to report, by construction.
        status, body = _parse(health_endpoint.HealthState(_App(clock=None)).response())
        assert "200" in status
        assert body["status"] == "healthy"
        assert body["observables"]["clock"] == "unstarted"

    def test_an_advancing_clock_is_healthy(self):
        clock = _Clock(ticks=5)
        state = health_endpoint.HealthState(_App(clock=clock))
        assert "200" in _parse(state.response())[0]
        clock.ticks = 6
        assert "200" in _parse(state.response())[0]

    def test_a_stuck_worker_503s_because_a_restart_repairs_it(self, monkeypatch):
        # Alive but not advancing: supervise() cannot see this — the task has
        # not exited — so it is the one failure this endpoint adds.
        clock = _Clock(ticks=5)
        state = health_endpoint.HealthState(_App(clock=clock))
        t = [1000.0]
        monkeypatch.setattr(health_endpoint, "monotonic", lambda: t[0])
        assert "200" in _parse(state.response())[0]  # first observation
        t[0] += 10.0 * health_endpoint.STALE_INTERVAL_MULTIPLE + 1
        status, body = _parse(state.response())  # ticks unchanged since
        assert "503" in status
        assert body["status"] == "unhealthy"
        assert body["stalled_seconds"] > 0

    def test_consecutive_failures_are_reported_but_do_NOT_gate(self):
        # Deliberate: a database blip would raise these on a worker that
        # recovers, and 503-ing would spend restartPolicyMaxRetries restarting
        # through an outage no restart fixes. Diagnostic, not a gate.
        clock = _Clock(ticks=5)
        clock.consecutive_failures = 99
        status, body = _parse(health_endpoint.HealthState(_App(clock=clock)).response())
        assert "200" in status
        assert body["observables"]["clock"]["consecutive_failures"] == 99

    def test_response_framing_is_valid_http(self):
        raw = health_endpoint.HealthState(_App(clock=_Clock(1))).response()
        head, _, body = raw.partition(b"\r\n\r\n")
        assert f"Content-Length: {len(body)}".encode() in head
        assert b"Content-Type: application/json" in head


class TestTheListenerBindsEarly:
    @pytest.mark.asyncio
    async def test_the_listener_answers_while_the_first_db_connection_is_still_blocking(
        self, monkeypatch
    ):
        """THE regression. Railway times the socket, not the program.

        Drives the real `run()` with an engine whose first `connect()` never
        returns, and asserts /health is already answering. On the pre-fix tree
        nothing binds the port and this cannot pass.
        """
        import src.worker as worker

        # Ephemeral port: a fixed one collides with a leaked listener from a
        # previous run and fails for a reason that has nothing to do with the
        # behaviour under test.
        monkeypatch.setenv("PORT", "0")
        blocked = asyncio.Event()

        class _Engine:
            async def connect(self):
                blocked.set()
                await asyncio.sleep(3600)  # the slow startup step, forever

        app = _App(clock=None)
        app.engine = _Engine()
        run_task = asyncio.create_task(worker.run(app, stop=asyncio.Event()))
        try:
            # Race the two: if run() dies before reaching connect(), surface ITS
            # exception rather than an opaque wait_for timeout.
            waiter = asyncio.create_task(blocked.wait())
            done, _ = await asyncio.wait(
                {waiter, run_task}, timeout=5, return_when=asyncio.FIRST_COMPLETED
            )
            if run_task in done:
                waiter.cancel()
                raise AssertionError(
                    f"run() exited before the first connect(): {run_task.exception()!r}"
                )
            assert waiter in done, "run() never reached the first connect()"

            port = app.health_server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /health HTTP/1.1\r\n\r\n")
            await writer.drain()
            raw = await asyncio.wait_for(reader.read(4096), timeout=5)
            writer.close()
            await writer.wait_closed()
            status, body = _parse(raw)
            assert "200" in status, f"health must answer during startup, got {status}"
            assert body["observables"]["clock"] == "unstarted"
        finally:
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)
            # run() creates the listener BEFORE its try/finally, so a failure or
            # cancellation during startup leaves it bound in-process. Harmless in
            # production (the process exits and the OS reclaims the port) but it
            # strands the port across tests, so close it here. Hardening run()'s
            # own cleanup is a follow-up, noted in the PR.
            srv = getattr(app, "health_server", None)
            if srv is not None:
                srv.close()
                await srv.wait_closed()
