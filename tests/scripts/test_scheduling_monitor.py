"""The poller's decisions, which are pure so they can be tested rather than staked out.

The load-bearing tests here are the `NO_SIGNAL` ones. A poller that reports
"nothing is stalled" on an estate with zero destinations is a green light wired
to nothing. Every other property in this file is ordinary; that one is the
reason the file exists.

What a reading means is `classify`'s, and is pinned in
`tests/storydump_cli/test_health_classify_scheduling.py`. These tests build a
verdict and pin what the poller does with it: what it says, when it repeats,
what it records, and what `main` prints and exits with.
"""

from __future__ import annotations

import json

from scripts import scheduling_monitor as monitor
from storydump_cli.health_classify.scheduling import (
    DEFAULT_STALL_THRESHOLD_S,
    DEFAULT_WORKER_OVERDUE_S,
    DEFAULT_WORKER_STALE_S,
    HEALTHY,
    NO_SIGNAL,
    STALLED,
    WORKER_DOWN,
    WORKER_UNKNOWN,
    Verdict,
    classify,
)

T = 600


def step(verdict, prior, now):
    """Exactly what `main` does: decide, then record the announcement ONLY if a
    message actually went out.

    Mirrored here rather than hand-rolled, because the two-field split
    (`state` = what the endpoint said, `announced` = what a human was told) is
    the thing under test. A test that advanced `announced` unconditionally would
    pass against a poller that records speaking whether or not delivery worked.
    """
    state, msg = monitor.decide(verdict, prior, now)
    if msg is not None:
        state = monitor.announce(state, verdict, now)
    return state, msg


#: A worker block that is provably alive — what the endpoint serves.
#: Kept as the DEFAULT so the tests below go on testing the cursor states: a
#: payload with no worker block is `UNREACHABLE`, whatever the cursor says.
#: The age is a healthy one against the 60 s recurring beat — well inside
#: `DEFAULT_WORKER_STALE_S`, which is what "provably alive" has to mean here.
ALIVE_WORKER = {
    "succeeded_ever": 78,
    "last_success_age_seconds": 45,
    "overdue_ready": 0,
    "max_overdue_seconds": None,
}


def body(stalled=0, active=0, lag=None, worker=ALIVE_WORKER):
    """The endpoint payload. `worker=None` omits the worker block."""
    data = {"stalled": stalled, "accounts_active": active, "max_lag_seconds": lag}
    if worker is not None:
        data["worker"] = worker
    return json.dumps(data)


class TestNothingToSeeIsNotTheSameAsNothingWrong:
    """*Nothing is late* and *nothing EXISTS to be late* have opposite remedies
    and must never render the same. The second is `NO_SIGNAL`: it is said, it
    never pages, it repeats rarely, and the end of it is announced."""

    def test_no_signal_never_pages(self):
        _, msg = step(classify(200, body(active=0), threshold_s=T), {}, now=1000.0)
        assert msg is not None, "silence would let a reader believe they are covered"
        assert "FLEET ALERT" not in msg, "an empty estate is expected, not a fault"
        assert "all-clear" in msg.lower() or "cannot detect" in msg.lower()

    def test_it_says_so_once_then_stops(self):
        v = classify(200, body(active=0), threshold_s=T)
        state, first = step(v, {}, now=1000.0)
        assert first is not None
        _, second = step(v, state, now=1000.0 + 3600)
        assert second is None

    def test_but_it_does_not_stay_quiet_forever(self):
        # "We have no monitoring coverage" must not fade from memory because it
        # was said once a month ago.
        v = classify(200, body(active=0), threshold_s=T)
        state, _ = step(v, {}, now=1000.0)
        _, later = step(v, state, now=1000.0 + monitor.RENOTICE_NO_SIGNAL_AFTER_S + 1)
        assert later is not None

    def test_the_day_it_gains_sight_it_says_so(self):
        # Otherwise the transition out of blindness is invisible, and nobody
        # knows when the monitor started being worth anything.
        state, _ = step(classify(200, body(active=0), threshold_s=T), {}, now=1.0)
        _, msg = step(
            classify(200, body(active=2, lag=5), threshold_s=T), state, now=2.0
        )
        assert msg is not None and "SIGNAL ACQUIRED" in msg


class TestConfirmationIsAsymmetricBecauseTheSIGNALSAre:
    """`STALLED` on the first reading, `UNREACHABLE` on the second. Not two
    tunings of one knob: a lag past the threshold already CONTAINS its duration,
    while a failed request contains none."""

    def test_stalled_alerts_immediately(self):
        _, msg = step(classify(200, body(2, 5, 900), threshold_s=T), {}, now=1.0)
        assert msg is not None and "FLEET ALERT" in msg

    def test_unreachable_holds_its_tongue_once(self):
        v = classify(0, "boom", threshold_s=T)
        state, first = step(v, {}, now=1.0)
        assert first is None, "one failed request is a dropped packet"
        _, second = step(v, state, now=2.0)
        assert second is not None and "FLEET ALERT" in second

    def test_an_intermittent_blip_never_pages(self):
        # fail, recover, fail — the run counter must reset, or a flaky network
        # eventually pages on two failures that were never consecutive.
        bad, good = (
            classify(0, "x", threshold_s=T),
            classify(200, body(0, 5, 1), threshold_s=T),
        )
        state, _ = step(bad, {}, now=1.0)
        state, _ = step(good, state, now=2.0)
        state, msg = step(bad, state, now=3.0)
        assert msg is None
        assert state["consecutive"] == 1


class TestItRepeatsWhileBrokenAndAnnouncesRecovery:
    def test_a_persisting_stall_does_not_go_quiet_forever(self):
        v = classify(200, body(2, 5, 5000), threshold_s=T)
        state, _ = step(v, {}, now=1000.0)
        _, soon = step(v, state, now=1000.0 + monitor.REALERT_AFTER_S - 1)
        assert soon is None
        _, later = step(v, state, now=1000.0 + monitor.REALERT_AFTER_S + 1)
        assert later is not None

    def test_recovery_is_announced(self):
        # Without this, "the alerts stopped" is ambiguous between FIXED and
        # MONITOR DIED — the same collapse the endpoint's 503 branch prevents.
        state, _ = step(classify(200, body(2, 5, 900), threshold_s=T), {}, now=1.0)
        _, msg = step(classify(200, body(0, 5, 3), threshold_s=T), state, now=2.0)
        assert msg is not None and "RECOVERED" in msg

    def test_a_quiet_healthy_run_says_nothing(self):
        v = classify(200, body(0, 5, 3), threshold_s=T)
        state, _ = step(v, {}, now=1.0)
        _, msg = step(v, state, now=2.0)
        assert msg is None


class TestTheStateFile:
    def test_a_corrupt_file_reads_as_no_history(self, tmp_path):
        p = tmp_path / "s.json"
        p.write_text("{not json")
        assert monitor.load_state(str(p)) == {}

    def test_a_missing_file_reads_as_no_history(self, tmp_path):
        assert monitor.load_state(str(tmp_path / "nope.json")) == {}

    def test_a_round_trip_survives(self, tmp_path):
        p = str(tmp_path / "s.json")
        monitor.save_state(p, {"state": NO_SIGNAL, "consecutive": 3})
        assert monitor.load_state(p)["consecutive"] == 3

    def test_no_temp_files_are_left_behind(self, tmp_path):
        p = str(tmp_path / "s.json")
        monitor.save_state(p, {"state": HEALTHY})
        assert [f.name for f in tmp_path.iterdir()] == ["s.json"]


class TestMainEndToEnd:
    """Driven through `main` with the network and the notifier stubbed at the
    seam, so the argument parsing, exit codes and state persistence are exercised
    rather than assumed."""

    def _run(self, monkeypatch, tmp_path, status, raw, notify_ok=True, calls=None):
        monkeypatch.setattr(monitor, "fetch", lambda url, timeout_s: (status, raw))
        monkeypatch.setattr(
            monitor,
            "notify",
            lambda command, message: (
                (calls.append(message) if calls is not None else None) or notify_ok
            ),
        )
        return monitor.main(
            [
                "--url",
                "http://x/health/scheduling",
                "--state-file",
                str(tmp_path / "s.json"),
                "--notify-command",
                "/bin/true",
            ]
        )

    def test_no_signal_speaks_once_then_is_quiet(self, monkeypatch, tmp_path):
        sent = []
        assert (
            self._run(monkeypatch, tmp_path, 200, body(active=0), calls=sent)
            == monitor.EXIT_SPOKE
        )
        assert (
            self._run(monkeypatch, tmp_path, 200, body(active=0), calls=sent)
            == monitor.EXIT_QUIET
        )
        assert len(sent) == 1

    def test_a_stall_reaches_the_notifier(self, monkeypatch, tmp_path):
        sent = []
        assert (
            self._run(monkeypatch, tmp_path, 200, body(3, 9, 4000), calls=sent)
            == monitor.EXIT_SPOKE
        )
        assert "FLEET ALERT" in sent[0]

    def test_a_failed_notify_exits_nonzero_and_does_not_record_speaking(
        self, monkeypatch, tmp_path
    ):
        # A monitor cannot page about its own paging failure. What it CAN do is
        # refuse to believe it spoke, so the next poll tries again instead of
        # de-duplicating against a message nobody received.
        rc = self._run(monkeypatch, tmp_path, 200, body(active=0), notify_ok=False)
        assert rc == monitor.EXIT_NOTIFY_FAILED
        st = monitor.load_state(str(tmp_path / "s.json"))
        assert st["spoke_at"] == 0.0
        assert "notify_failed_at" in st
        assert (
            self._run(monkeypatch, tmp_path, 200, body(active=0)) == monitor.EXIT_SPOKE
        )

    def test_a_failed_recovery_notice_is_retried_on_the_next_poll(
        self, monkeypatch, tmp_path
    ):
        """THE CASE THE TWO-FIELD SPLIT EXISTS FOR.

        A stall alerts. Scheduling recovers. The RECOVERED notice fails to send.
        If the history recorded only what the ENDPOINT last said, the next poll
        sees healthy-after-healthy and stays silent forever — and "the alerts
        stopped" collapses back into ambiguity between *fixed* and *monitor
        died*, which is the exact thing RECOVERED exists to prevent.

        Keyed on what a human was last TOLD, the notice is re-derived and sent.
        """
        sent = []
        assert (
            self._run(monkeypatch, tmp_path, 200, body(2, 5, 4000), calls=sent)
            == monitor.EXIT_SPOKE
        )
        assert "FLEET ALERT" in sent[0]

        rc = self._run(monkeypatch, tmp_path, 200, body(0, 5, 2), notify_ok=False)
        assert rc == monitor.EXIT_NOTIFY_FAILED
        assert monitor.load_state(str(tmp_path / "s.json"))["announced"] == STALLED

        assert (
            self._run(monkeypatch, tmp_path, 200, body(0, 5, 2), calls=sent)
            == monitor.EXIT_SPOKE
        )
        assert "RECOVERED" in sent[-1]

    def test_status_prints_the_last_state_without_polling(
        self, monkeypatch, tmp_path, capsys
    ):
        def explode(*a, **k):  # pragma: no cover - must never run
            raise AssertionError("--status polled the endpoint")

        monkeypatch.setattr(monitor, "fetch", explode)
        monitor.save_state(str(tmp_path / "s.json"), {"state": NO_SIGNAL})
        assert (
            monitor.main(
                ["--url", "x", "--state-file", str(tmp_path / "s.json"), "--status"]
            )
            == monitor.EXIT_QUIET
        )
        assert NO_SIGNAL in capsys.readouterr().out

    def test_a_missing_worker_block_is_unreachable_and_pages_on_the_second_poll(
        self, monkeypatch, tmp_path, capsys
    ):
        """A reading with no worker block is a broken instrument, and `main`
        must treat it as one.

        With a destination active and no lag, the cursor axis alone would answer
        `healthy` and the worker axis would be gone without a word. So the first
        poll prints `unreachable` and stays quiet (one reading is not enough),
        the second pages, and neither prints `healthy`.
        """
        alert = "FLEET ALERT: storydump scheduling check UNREACHABLE"
        raw = body(stalled=0, active=1, lag=0, worker=None)
        sent = []

        rc = self._run(monkeypatch, tmp_path, 200, raw, calls=sent)
        out = capsys.readouterr().out
        assert rc == monitor.EXIT_QUIET
        assert any(line.startswith("unreachable: ") for line in out.splitlines())
        assert "healthy" not in out.lower()
        assert sent == []

        rc = self._run(monkeypatch, tmp_path, 200, raw, calls=sent)
        out = capsys.readouterr().out
        assert rc == monitor.EXIT_SPOKE
        assert "healthy" not in out.lower()
        assert len(sent) == 1
        assert sent[0].startswith(alert)


# ── the worker-liveness axis ─────────────────────────────────────────────────
#
# `NO_SIGNAL` is the honest answer for the CURSOR axis on an empty estate, and
# it is also the answer a DEAD WORKER produces there. These tests pin how the
# poller treats the worker states: an outage pages at once, and blindness is
# said but never pages.


def test_worker_down_pages_on_the_very_first_reading():
    """An outage, treated like `STALLED` and deliberately not like `UNREACHABLE`.

    `UNREACHABLE` waits for a second reading because one failed request is a
    dropped packet. This is not a request failure — the endpoint answered, and
    what it said is that no system job has finished. There is no dropped-packet
    reading of that, so waiting a cadence only lengthens an outage.
    """
    verdict = Verdict(WORKER_DOWN, "no system job has finished for 90000s")

    _, msg = monitor.decide(verdict, {}, now=1000.0)

    assert msg is not None, "a dead worker stayed silent on its first reading"
    assert "FLEET ALERT" in msg


def test_worker_unknown_is_a_notice_and_never_pages():
    """Blind is not broken. It must be SAID — repeatedly, so it cannot fade —
    but it is not an outage and must not wake anyone."""
    verdict = Verdict(WORKER_UNKNOWN, "no baseline")

    _, msg = monitor.decide(verdict, {}, now=1000.0)

    assert msg is not None, "total blindness said nothing at all"
    assert "FLEET ALERT" not in msg


# ── the threshold flags ──────────────────────────────────────────────────────
#
# The classifier decides what a reading means; the poller only hands it the
# thresholds. These tests pin that the hand-off is real: the defaults are the
# classifier's own, and a flag reaches `classify`.


def test_the_threshold_flags_default_to_the_classifiers_constants():
    """This is what makes `tests/src/test_worker.py`'s beat test
    (`test_the_monitors_stale_threshold_spans_three_beats_of_the_fastest_kind`)
    cover the poller.

    That test holds `DEFAULT_WORKER_STALE_S` to three beats of the worker's
    fastest recurring kind. It says nothing about the poller unless the poller
    runs with that very constant when `--worker-stale-threshold` is not given,
    so each threshold flag is pinned to its constant in
    `storydump_cli/health_classify/scheduling.py`.
    """
    args = monitor.build_parser().parse_args(
        ["--url", "http://x", "--state-file", "s.json"]
    )

    assert args.stall_threshold == DEFAULT_STALL_THRESHOLD_S
    assert args.worker_stale_threshold == DEFAULT_WORKER_STALE_S
    assert args.worker_overdue_threshold == DEFAULT_WORKER_OVERDUE_S


def test_the_worker_thresholds_are_reachable_from_the_command_line(
    monkeypatch, tmp_path
):
    """Adding the flags and forgetting to thread them into `classify` is the
    ordinary way this breaks: the defaults keep every test green while the
    operator's setting silently does nothing.

    So this drives `main` with an age that is healthy under the DEFAULT and an
    outage only under the flag. It can only pass if the value travelled.
    """
    sent = []
    # Derived from the default, so both halves hold whatever it becomes.
    age = DEFAULT_WORKER_STALE_S // 2
    raw = body(
        active=0,
        worker={**ALIVE_WORKER, "last_success_age_seconds": age},
    )
    monkeypatch.setattr(monitor, "fetch", lambda url, timeout_s: (200, raw))
    monkeypatch.setattr(
        monitor, "notify", lambda command, message: sent.append(message) or True
    )

    assert classify(200, raw, threshold_s=T).state == NO_SIGNAL

    rc = monitor.main(
        [
            "--url",
            "http://x/health/scheduling",
            "--state-file",
            str(tmp_path / "s.json"),
            "--notify-command",
            "/bin/true",
            "--worker-stale-threshold",
            str(age // 2),
        ]
    )

    assert rc == monitor.EXIT_SPOKE, "the flag did not reach classify"
    assert sent and "WORKER IS NOT ADVANCING" in sent[0]
