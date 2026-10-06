"""The delivery monitor's decisions (#1482), pure so they can be tested rather
than staked out, in `test_posting_monitor.py`'s shape.

The contract: it FIRES at 5 failed or ambiguous deliveries in the hour, on the
first such reading; it CLEARS only after two consecutive polls at 1 or fewer;
between the two it holds whatever it last said; and while failing it REPEATS
every 6 hours. The thresholds are read from the module's defaults, but every
clock and every reading below is a literal, so a mutant that moves a number
moves a verdict here rather than moving the test with it.
"""

from __future__ import annotations

import json

import pytest

from scripts.delivery_monitor import (
    CLEARING,
    DEFAULT_CLEAR_AT,
    DEFAULT_RAISE_AT,
    DELIVERING,
    EXIT_NOTIFY_FAILED,
    EXIT_QUIET,
    EXIT_SPOKE,
    FAILING,
    UNREACHABLE,
    announce,
    classify,
    decide,
)
from scripts.posting_monitor import load_state, save_state

HOUR = 3600
T0 = 1_000_000.0
#: The timer's cadence.
POLL = 300.0


def payload(n, *, sent=40, by_class=None):
    """A well-formed `/health/delivery` body with *n* alerting rows."""
    if by_class is None:
        by_class = (
            {"destination_gone": {"rows": n, "alerting": n, "codes": {"403": n}}}
            if n
            else {}
        )
    return json.dumps(
        {
            "window_seconds": HOUR,
            "sent_in_window": sent,
            "failed_or_ambiguous": n,
            "by_class": by_class,
        }
    )


def read(n, **kw):
    return classify(
        200, payload(n, **kw), raise_at=DEFAULT_RAISE_AT, clear_at=DEFAULT_CLEAR_AT
    )


UNREACHABLE_READING = classify(
    503, "", raise_at=DEFAULT_RAISE_AT, clear_at=DEFAULT_CLEAR_AT
)


def step(verdict, prior, now):
    """Exactly what `main` does: decide, then record the announcement only if
    a message went out. A helper that advanced `announced` unconditionally
    would pass against the bug the two-field split exists to prevent."""
    state, message, subject = decide(verdict, prior, now)
    if message is not None:
        state = announce(state, subject, now)
    return state, message


def run(readings, *, prior=None, start=T0):
    """The readings, one poll apart; returns the last state and every message."""
    state, said = dict(prior or {}), []
    for i, verdict in enumerate(readings):
        state, message = step(verdict, state, start + i * POLL)
        said.append(message)
    return state, said


class TestItFiresAtFive:
    def test_four_is_quiet_and_five_fires_on_the_first_reading(self):
        state, said = run([read(4)])
        assert said == [None] and state["effective"] == DELIVERING
        state, said = run([read(5)])
        assert said[0].startswith(
            "FLEET ALERT: storydump OUTBOX DELIVERIES ARE FAILING"
        )
        assert state["effective"] == FAILING and state["announced"] == FAILING

    def test_a_rise_through_the_band_fires_when_it_reaches_five(self):
        _, said = run([read(2), read(4), read(5)])
        assert said[:2] == [None, None] and said[2].startswith("FLEET ALERT")

    def test_the_alert_names_the_classes_codes_and_context(self):
        by_class = {
            "destination_gone": {"rows": 5, "alerting": 5, "codes": {"403": 5}},
            "refused": {"rows": 2, "alerting": 2, "codes": {"400": 2}},
            "rate_limited": {"rows": 3, "alerting": 0, "codes": {"429": 3}},
        }
        _, (message,) = run([read(7, by_class=by_class)])
        assert "7 failed or ambiguous in the last 1h" in message
        assert "destination_gone ×5 (403 ×5), refused ×2 (400 ×2)" in message
        assert "context: rate_limited ×3 (429 ×3)" in message
        assert "40 sent in the same window" in message


class TestTheBandHolds:
    def test_a_failure_in_the_band_stays_failing_and_says_nothing(self):
        state, said = run([read(5), read(4), read(3), read(2)])
        assert said[1:] == [None, None, None]
        assert state["effective"] == FAILING

    def test_a_quiet_estate_in_the_band_stays_quiet(self):
        state, said = run([read(2), read(3), read(4)])
        assert said == [None, None, None] and state["effective"] == DELIVERING


class TestItClearsOnlyAfterTwoPollsAtOneOrFewer:
    def test_one_quiet_poll_is_not_a_recovery(self):
        state, said = run([read(5), read(1)])
        assert said[1] is None and state["effective"] == CLEARING

    def test_the_second_quiet_poll_recovers_and_says_so(self):
        state, said = run([read(5), read(1), read(0)])
        assert said[1] is None
        assert said[2].startswith("RECOVERED: storydump outbox deliveries")
        assert state["effective"] == DELIVERING and state["announced"] == DELIVERING

    def test_two_is_not_quiet_enough(self):
        state, said = run([read(5), read(2), read(2), read(2)])
        assert said[1:] == [None, None, None] and state["effective"] == FAILING

    def test_a_band_reading_between_quiet_polls_restarts_the_dwell(self):
        state, said = run([read(5), read(1), read(3), read(1)])
        assert said[1:] == [None, None, None] and state["effective"] == CLEARING
        state, (message,) = run([read(1)], prior=state, start=T0 + 4 * POLL)
        assert message.startswith("RECOVERED") and state["effective"] == DELIVERING

    def test_after_an_unreachable_spell_a_quiet_reading_never_pages_failing(self):
        """`announced` is UNREACHABLE by then: a reading at or under the clear
        line is clearing, which never pages, not a failure to tell again."""
        state, said = run(
            [read(5), UNREACHABLE_READING, UNREACHABLE_READING, read(1), read(1)]
        )
        assert said[0].startswith("FLEET ALERT") and said[1] is None
        assert "UNREACHABLE" in said[2]
        assert said[3] is None, "a reading of 1 paged as a failure"
        assert said[4].startswith("RECOVERED") and state["effective"] == DELIVERING

    def test_a_failure_that_returns_after_an_unreachable_spell_is_said_again(self):
        _, said = run([read(5), UNREACHABLE_READING, UNREACHABLE_READING, read(6)])
        assert said[3].startswith(
            "FLEET ALERT: storydump OUTBOX DELIVERIES ARE FAILING"
        )


class TestItRepeatsEverySixHours:
    def test_a_standing_failure_repeats_at_six_hours_and_not_before(self):
        state, first = step(read(9), {}, T0)
        assert first.startswith("FLEET ALERT")
        state, early = step(read(9), state, T0 + 6 * HOUR - POLL)
        assert early is None
        _, again = step(read(9), state, T0 + 6 * HOUR)
        assert again.startswith("FLEET ALERT")

    def test_a_failure_held_in_the_band_repeats_too(self):
        """Still failing six hours on is said again, whatever the reading in
        the band: the hysteresis holds the verdict, not the silence."""
        state, _ = step(read(9), {}, T0)
        _, again = step(read(3), state, T0 + 6 * HOUR)
        assert again.startswith("FLEET ALERT")

    def test_a_failed_notify_is_said_again_on_the_next_poll(self):
        state, message, _ = decide(read(5), {}, T0)  # never announced
        assert message is not None and state["announced"] is None
        _, again = step(read(5), state, T0 + POLL)
        assert again.startswith("FLEET ALERT")


class TestUnreachable:
    def test_one_failed_poll_is_silent_and_the_second_speaks(self):
        state, said = run([UNREACHABLE_READING, UNREACHABLE_READING])
        assert said[0] is None
        assert "UNREACHABLE" in said[1] and "cannot look" in said[1]
        assert state["effective"] == DELIVERING, "it knows nothing new"

    @pytest.mark.parametrize(
        "body",
        [
            "not json",
            "[]",
            json.dumps({"sent_in_window": 1, "window_seconds": 3600, "by_class": {}}),
            json.dumps(
                {
                    "failed_or_ambiguous": True,
                    "sent_in_window": 1,
                    "window_seconds": 3600,
                    "by_class": {},
                }
            ),
            json.dumps(
                {
                    "failed_or_ambiguous": 0,
                    "sent_in_window": 1,
                    "window_seconds": 3600,
                    "by_class": [],
                }
            ),
        ],
        ids=["not json", "not an object", "a key missing", "a bool count", "a list"],
    )
    def test_a_malformed_body_is_unreachable_never_a_quiet_hour(self, body):
        verdict = classify(
            200, body, raise_at=DEFAULT_RAISE_AT, clear_at=DEFAULT_CLEAR_AT
        )
        assert verdict.state == UNREACHABLE

    def test_a_transport_failure_names_itself(self):
        verdict = classify(
            0, "connection refused", raise_at=DEFAULT_RAISE_AT, clear_at=1
        )
        assert verdict.state == UNREACHABLE and "connection refused" in verdict.detail


class TestMainEndToEnd:
    """The wiring through `main`, with the network and the pager stubbed: the
    thresholds reaching `classify`, the state file, the exit codes."""

    @staticmethod
    def _run(monkeypatch, tmp_path, body, argv_extra=(), status=200, notify_ok=True):
        import scripts.delivery_monitor as mod

        sent = []
        monkeypatch.setattr(mod, "fetch", lambda url, t: (status, body))
        monkeypatch.setattr(
            mod, "notify", lambda cmd, msg: (sent.append(msg), notify_ok)[1]
        )
        state_file = str(tmp_path / "state.json")
        rc = mod.main(
            [
                "--url",
                "http://example.invalid/health/delivery",
                "--state-file",
                state_file,
                "--notify-command",
                "/bin/true",
                *argv_extra,
            ]
        )
        return rc, sent, load_state(state_file)

    def test_a_quiet_hour_exits_quiet_and_pages_nobody(self, monkeypatch, tmp_path):
        rc, sent, state = self._run(monkeypatch, tmp_path, payload(0))
        assert rc == EXIT_QUIET and sent == [] and state["effective"] == DELIVERING

    def test_five_pages_and_exits_spoke(self, monkeypatch, tmp_path):
        rc, sent, state = self._run(monkeypatch, tmp_path, payload(5))
        assert rc == EXIT_SPOKE and len(sent) == 1
        assert state["announced"] == FAILING

    def test_a_failed_pager_exits_nonzero_and_leaves_announced_alone(
        self, monkeypatch, tmp_path
    ):
        rc, _, state = self._run(monkeypatch, tmp_path, payload(5), notify_ok=False)
        assert rc == EXIT_NOTIFY_FAILED
        assert state["announced"] is None and "notify_failed_at" in state

    def test_the_thresholds_are_reachable_from_the_command_line(
        self, monkeypatch, tmp_path
    ):
        rc, sent, _ = self._run(monkeypatch, tmp_path, payload(3))
        assert rc == EXIT_QUIET and sent == []
        rc, sent, state = self._run(
            monkeypatch, tmp_path, payload(3), argv_extra=["--raise-at", "3"]
        )
        assert rc == EXIT_SPOKE and state["effective"] == FAILING

    def test_a_clear_at_or_over_the_raise_is_refused(self, monkeypatch, tmp_path):
        with pytest.raises(SystemExit) as exc:
            self._run(
                monkeypatch,
                tmp_path,
                payload(0),
                argv_extra=["--raise-at", "3", "--clear-at", "3"],
            )
        assert exc.value.code == 2

    def test_the_dwell_is_carried_across_runs_by_the_state_file(
        self, monkeypatch, tmp_path
    ):
        """Three runs against ONE state file: the failure, one quiet poll
        that is not yet a recovery, and the second that is."""
        rc, _, _ = self._run(monkeypatch, tmp_path, payload(5))
        assert rc == EXIT_SPOKE
        rc, sent, state = self._run(monkeypatch, tmp_path, payload(1))
        assert rc == EXIT_QUIET and state["effective"] == CLEARING
        rc, sent, state = self._run(monkeypatch, tmp_path, payload(1))
        assert rc == EXIT_SPOKE and sent[-1].startswith("RECOVERED")

    def test_status_prints_the_last_state_without_polling(
        self, monkeypatch, tmp_path, capsys
    ):
        import scripts.delivery_monitor as mod

        state_file = str(tmp_path / "state.json")
        save_state(state_file, {"effective": FAILING, "detail": "recorded earlier"})

        def _boom(*a, **k):  # pragma: no cover - must never run
            raise AssertionError("--status must not poll")

        monkeypatch.setattr(mod, "fetch", _boom)
        assert (
            mod.main(["--url", "u", "--state-file", state_file, "--status"])
            == EXIT_QUIET
        )
        assert json.loads(capsys.readouterr().out)["effective"] == FAILING
