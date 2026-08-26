"""The poller's decisions, which are pure so they can be tested rather than staked out.

The load-bearing tests here are the `NO_SIGNAL` ones. A poller that reports
"nothing is stalled" on an estate with zero destinations is a green light wired
to nothing, and it would have stayed green through the whole 19-hour outage had
the estate been empty then. Every other property in this file is ordinary; that
one is the reason the file exists.
"""

from __future__ import annotations

import json

import pytest

from scripts.scheduling_monitor import (
    EXIT_NOTIFY_FAILED,
    EXIT_QUIET,
    EXIT_SPOKE,
    HEALTHY,
    NO_SIGNAL,
    REALERT_AFTER_S,
    RENOTICE_NO_SIGNAL_AFTER_S,
    STALLED,
    UNREACHABLE,
    classify,
    decide,
    announce,
    load_state,
    save_state,
)

T = 600


def step(verdict, prior, now):
    """Exactly what `main` does: decide, then record the announcement ONLY if a
    message actually went out.

    Mirrored here rather than hand-rolled, because the two-field split
    (`state` = what the endpoint said, `announced` = what a human was told) is
    the thing under test. A test that advanced `announced` unconditionally would
    pass against the very bug this split fixes.
    """
    state, msg = decide(verdict, prior, now)
    if msg is not None:
        state = announce(state, verdict, now)
    return state, msg


def body(stalled=0, active=0, lag=None):
    return json.dumps(
        {"stalled": stalled, "accounts_active": active, "max_lag_seconds": lag}
    )


class TestNothingToSeeIsNotTheSameAsNothingWrong:
    """The distinction the whole module exists for: *nothing is late* and
    *nothing EXISTS to be late* have opposite remedies and must never render the
    same."""

    def test_an_empty_estate_is_no_signal_and_never_healthy(self):
        v = classify(200, body(stalled=0, active=0, lag=None), threshold_s=T)
        assert v.state == NO_SIGNAL
        assert v.state != HEALTHY

    def test_the_same_numbers_with_destinations_present_are_healthy(self):
        # `stalled: 0, max_lag_seconds: null` is byte-identical in both cases.
        # ONLY `accounts_active` separates them, which is why it is in the
        # payload at all.
        v = classify(200, body(stalled=0, active=3, lag=None), threshold_s=T)
        assert v.state == HEALTHY

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
        _, later = step(v, state, now=1000.0 + RENOTICE_NO_SIGNAL_AFTER_S + 1)
        assert later is not None

    def test_the_day_it_gains_sight_it_says_so(self):
        # Otherwise the transition out of blindness is invisible, and nobody
        # knows when the monitor started being worth anything.
        state, _ = step(classify(200, body(active=0), threshold_s=T), {}, now=1.0)
        _, msg = step(
            classify(200, body(active=2, lag=5), threshold_s=T), state, now=2.0
        )
        assert msg is not None and "SIGNAL ACQUIRED" in msg


class TestAMalformedAnswerIsNeverAHealthyOne:
    """The lenient spelling — `data.get("stalled", 0)` — turns a broken
    instrument into a clean bill of health. That is this poller's own failure
    mode, one layer inward."""

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "not json",
            "[]",
            '"a string"',
            "{}",
            '{"stalled": 0}',
            '{"accounts_active": 1}',
            '{"stalled": "0", "accounts_active": 1, "max_lag_seconds": null}',
            '{"stalled": 0, "accounts_active": null, "max_lag_seconds": null}',
            '{"stalled": 0, "accounts_active": 1, "max_lag_seconds": "700"}',
            '{"stalled": true, "accounts_active": 1, "max_lag_seconds": null}',
        ],
    )
    def test_every_malformed_shape_is_unreachable(self, raw):
        assert classify(200, raw, threshold_s=T).state == UNREACHABLE

    @pytest.mark.parametrize("status", [0, 404, 500, 502, 503])
    def test_every_non_200_is_unreachable(self, status):
        assert classify(status, body(active=5), threshold_s=T).state == UNREACHABLE


class TestTheThreshold:
    def test_a_lag_at_the_threshold_is_not_yet_stalled(self):
        assert classify(200, body(1, 5, T), threshold_s=T).state == HEALTHY

    def test_one_second_past_it_is(self):
        assert classify(200, body(1, 5, T + 1), threshold_s=T).state == STALLED

    def test_a_null_lag_with_destinations_is_healthy(self):
        assert classify(200, body(0, 5, None), threshold_s=T).state == HEALTHY


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
        _, soon = step(v, state, now=1000.0 + REALERT_AFTER_S - 1)
        assert soon is None
        _, later = step(v, state, now=1000.0 + REALERT_AFTER_S + 1)
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
        assert load_state(str(p)) == {}

    def test_a_missing_file_reads_as_no_history(self, tmp_path):
        assert load_state(str(tmp_path / "nope.json")) == {}

    def test_a_round_trip_survives(self, tmp_path):
        p = str(tmp_path / "s.json")
        save_state(p, {"state": NO_SIGNAL, "consecutive": 3})
        assert load_state(p)["consecutive"] == 3

    def test_no_temp_files_are_left_behind(self, tmp_path):
        p = str(tmp_path / "s.json")
        save_state(p, {"state": HEALTHY})
        assert [f.name for f in tmp_path.iterdir()] == ["s.json"]


class TestMainEndToEnd:
    """Driven through `main` with the network and the notifier stubbed at the
    seam, so the argument parsing, exit codes and state persistence are exercised
    rather than assumed."""

    def _run(self, monkeypatch, tmp_path, status, raw, notify_ok=True, calls=None):
        import scripts.scheduling_monitor as m

        monkeypatch.setattr(m, "fetch", lambda url, timeout: (status, raw))
        monkeypatch.setattr(
            m,
            "notify",
            lambda cmd, msg: (
                (calls.append(msg) if calls is not None else None) or notify_ok
            ),
        )
        return m.main(
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
            == EXIT_SPOKE
        )
        assert (
            self._run(monkeypatch, tmp_path, 200, body(active=0), calls=sent)
            == EXIT_QUIET
        )
        assert len(sent) == 1

    def test_a_stall_reaches_the_notifier(self, monkeypatch, tmp_path):
        sent = []
        assert (
            self._run(monkeypatch, tmp_path, 200, body(3, 9, 4000), calls=sent)
            == EXIT_SPOKE
        )
        assert "FLEET ALERT" in sent[0]

    def test_a_failed_notify_exits_nonzero_and_does_not_record_speaking(
        self, monkeypatch, tmp_path
    ):
        # A monitor cannot page about its own paging failure. What it CAN do is
        # refuse to believe it spoke, so the next poll tries again instead of
        # de-duplicating against a message nobody received.
        rc = self._run(monkeypatch, tmp_path, 200, body(active=0), notify_ok=False)
        assert rc == EXIT_NOTIFY_FAILED
        st = load_state(str(tmp_path / "s.json"))
        assert st["spoke_at"] == 0.0
        assert "notify_failed_at" in st
        assert self._run(monkeypatch, tmp_path, 200, body(active=0)) == EXIT_SPOKE

    def test_a_failed_recovery_notice_is_retried_on_the_next_poll(
        self, monkeypatch, tmp_path
    ):
        """THE CASE THE TWO-FIELD SPLIT EXISTS FOR, and the one my first version
        got wrong.

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
            == EXIT_SPOKE
        )
        assert "FLEET ALERT" in sent[0]

        rc = self._run(monkeypatch, tmp_path, 200, body(0, 5, 2), notify_ok=False)
        assert rc == EXIT_NOTIFY_FAILED
        assert load_state(str(tmp_path / "s.json"))["announced"] == STALLED

        assert (
            self._run(monkeypatch, tmp_path, 200, body(0, 5, 2), calls=sent)
            == EXIT_SPOKE
        )
        assert "RECOVERED" in sent[-1]

    def test_status_prints_the_last_state_without_polling(
        self, monkeypatch, tmp_path, capsys
    ):
        import scripts.scheduling_monitor as m

        def explode(*a, **k):  # pragma: no cover - must never run
            raise AssertionError("--status polled the endpoint")

        monkeypatch.setattr(m, "fetch", explode)
        save_state(str(tmp_path / "s.json"), {"state": NO_SIGNAL})
        assert (
            m.main(["--url", "x", "--state-file", str(tmp_path / "s.json"), "--status"])
            == EXIT_QUIET
        )
        assert NO_SIGNAL in capsys.readouterr().out
