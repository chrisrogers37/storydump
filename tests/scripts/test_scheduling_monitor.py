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

import pytest

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


def step(reading, prior, now, decide=monitor.decide):
    """Exactly what `main` does on each axis: decide, then record the
    announcement ONLY if a message actually went out.

    Mirrored here rather than hand-rolled, because the two-field split
    (`state` = what the endpoint said, `announced` = what a human was told) is
    the thing under test. A test that advanced `announced` unconditionally would
    pass against a poller that records speaking whether or not delivery worked.
    """
    state, msg = decide(reading, prior, now)
    if msg is not None:
        state = monitor.announce(state, now)
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


def run_main(
    monkeypatch,
    tmp_path,
    status,
    raw,
    *,
    delivery=(404, ""),
    delivery_url="http://x/health/delivery",
    notify_ok=True,
    calls=None,
    extra=(),
):
    """`main` with the network and the notifier stubbed at the seam, keyed by
    URL: `(status, raw)` at --url and `delivery` at `delivery_url`, by default
    the surface beside it and a reading the delivery axis skips. Any other URL
    answers 404. The state file is `tmp_path / "s.json"`."""
    pages = {"http://x/health/scheduling": (status, raw), delivery_url: delivery}
    monkeypatch.setattr(
        monitor, "fetch", lambda url, timeout_s: pages.get(url, (404, ""))
    )
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
            *extra,
        ]
    )


#: A worker that has never finished a system job: no baseline and no backlog.
NO_BASELINE_WORKER = {
    **ALIVE_WORKER,
    "succeeded_ever": 0,
    "last_success_age_seconds": None,
}


@pytest.fixture(
    params=[(NO_SIGNAL, ALIVE_WORKER), (WORKER_UNKNOWN, NO_BASELINE_WORKER)],
    ids=["no-signal", "worker-unknown"],
)
def notice(request):
    """An empty estate's reading, with the worker provably alive or with no
    baseline at all: the two states that are said but never page."""
    state, worker = request.param
    verdict = classify(200, body(active=0, worker=worker), threshold_s=T)
    assert verdict.state == state
    return verdict


class TestNothingToSeeIsNotTheSameAsNothingWrong:
    """*Nothing is late* and *nothing EXISTS to be late* have opposite remedies
    and must never render the same. The second is `NO_SIGNAL`, or
    `WORKER_UNKNOWN` when the worker has no baseline either. Blind is not
    broken: each is said, never pages, repeats weekly rather than on the fault
    cadence, and the end of it is announced."""

    def test_it_is_said_and_never_pages(self, notice):
        _, msg = step(notice, {}, now=1000.0)
        assert msg is not None, "silence would let a reader believe they are covered"
        assert "FLEET ALERT" not in msg, "an empty estate is expected, not a fault"
        assert "all-clear" in msg.lower() or "cannot detect" in msg.lower()

    def test_it_says_so_once_then_stops(self, notice):
        # Checked past the 6-h fault cadence: a notice repeats weekly.
        state, first = step(notice, {}, now=1000.0)
        assert first is not None
        _, second = step(notice, state, now=1000.0 + monitor.REALERT_AFTER_S + 1)
        assert second is None

    def test_but_it_does_not_stay_quiet_forever(self, notice):
        # "We have no monitoring coverage" must not fade from memory because it
        # was said once a month ago. A week, as a number: quiet through the rest
        # of the week, said again after it.
        week = 7 * 24 * 3600
        state, _ = step(notice, {}, now=1000.0)
        _, soon = step(notice, state, now=1000.0 + week - 1)
        assert soon is None
        _, later = step(notice, state, now=1000.0 + week + 1)
        assert later is not None

    def test_the_day_it_gains_sight_it_says_so(self, notice):
        # Otherwise the transition out of blindness is invisible, and nobody
        # knows when the monitor started being worth anything.
        state, _ = step(notice, {}, now=1.0)
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


class TestLosingTheLastDestinationSpeaks:
    """An estate that HAD destinations and now has none is not the empty estate
    the first notice describes: nothing will be scheduled or posted until one
    comes back, and that is said daily, not once a week."""

    def test_an_estate_that_had_destinations_says_so_and_repeats_daily(self, notice):
        state, _ = step(classify(200, body(0, 1, 3), threshold_s=T), {}, now=1.0)
        state, msg = step(notice, state, now=2.0)
        assert msg is not None and "NO ACTIVE DESTINATIONS" in msg
        assert "had 1" in msg
        # A day, as a number: quiet through the 6-h fault cadence and the rest
        # of the day, said again after it.
        day = 24 * 3600
        _, soon = step(notice, state, now=2.0 + day - 1)
        assert soon is None
        _, later = step(notice, state, now=2.0 + day + 1)
        assert later is not None and "NO ACTIVE DESTINATIONS" in later

    def test_a_reading_with_no_payload_does_not_forget_the_estate(self, notice):
        # An unreachable reading carries no counts; what the estate had must
        # outlive it, or a network blip turns a lost destination back into the
        # weekly notice for an estate that never had one.
        state, _ = step(classify(200, body(0, 1, 3), threshold_s=T), {}, now=1.0)
        for now in (2.0, 3.0):
            state, _ = step(classify(0, "boom", threshold_s=T), state, now=now)
        _, msg = step(notice, state, now=4.0)
        assert msg is not None and "had 1" in msg


class TestAFaultThatEndsInBlindnessIsSaidHonestly:
    """A fault the human was told about, followed by a reading that can see
    nothing, closes that fault in words: the human must not be left holding an
    alert that no longer describes anything."""

    @pytest.mark.parametrize(
        "fault,readings",
        [
            pytest.param(
                lambda: Verdict(WORKER_DOWN, "no system job has finished for 90000s"),
                1,
                id="worker-down",
            ),
            pytest.param(
                lambda: classify(0, "boom", threshold_s=T), 2, id="unreachable"
            ),
        ],
    )
    def test_an_outage_that_goes_blind_says_it_recovered(self, fault, readings):
        v, state = fault(), {}
        for i in range(readings):
            state, _ = step(v, state, now=1.0 + i)
        assert state["announced"] == v.state
        _, msg = step(Verdict(WORKER_UNKNOWN, "no baseline"), state, now=10.0)
        assert msg is not None and msg.startswith(f"RECOVERED from {v.state}")

    def test_a_stall_that_goes_blind_is_not_called_recovered(self):
        # Pausing the only workspace stops the stall's evidence, not the stall.
        state, _ = step(classify(200, body(2, 5, 900), threshold_s=T), {}, now=1.0)
        _, msg = step(classify(200, body(active=0), threshold_s=T), state, now=2.0)
        assert msg is not None
        assert "the stall is no longer observable" in msg
        assert "RECOVERED" not in msg


#: `/health/delivery`'s body: the last hour's sends, and those that failed or
#: are ambiguous, by class and code.
QUIET_HOUR = {
    "window_seconds": 3600,
    "sent_in_window": 40,
    "failed_or_ambiguous": 0,
    "by_class": {},
}
FAILING_HOUR = {
    "window_seconds": 3600,
    "sent_in_window": 40,
    "failed_or_ambiguous": 3,
    "by_class": {
        "ambiguous": {"rows": 2, "alerting": 2, "codes": {"none": 2}},
        "refused": {"rows": 1, "alerting": 1, "codes": {"400": 1}},
        # A deferral: back to pending, so nothing in the class alerts.
        "rate_limited": {"rows": 4, "alerting": 0, "codes": {"429": 4}},
    },
}


def dstep(reading, prior, now):
    """`step` on the delivery axis's own state."""
    return step(reading, prior, now, decide=monitor.decide_delivery)


class TestDeliveryIsItsOwnAxis:
    """Cards failing and posts not landing are independent causes, so delivery
    is read from its own surface and judged with its own state: one axis never
    masks the other."""

    def test_failing_sends_page_with_their_counts(self):
        _, msg = dstep(FAILING_HOUR, {}, now=1.0)
        assert msg is not None and "FLEET ALERT" in msg and "DELIVERY" in msg
        assert "3 of the last hour's sends" in msg and "40 sent" in msg
        assert "ambiguous" in msg and "400" in msg
        assert "rate_limited" not in msg and "429" not in msg, "a deferral paged"

    def test_a_single_failed_send_pages(self):
        one = {
            **QUIET_HOUR,
            "failed_or_ambiguous": 1,
            "by_class": {"refused": {"rows": 1, "alerting": 1, "codes": {"400": 1}}},
        }
        _, msg = dstep(one, {}, now=1.0)
        assert msg is not None and "1 of the last hour's sends" in msg

    def test_a_quiet_hour_says_nothing(self):
        _, msg = dstep(QUIET_HOUR, {}, now=1.0)
        assert msg is None

    def test_it_repeats_while_failing_and_announces_recovery(self):
        state, _ = dstep(FAILING_HOUR, {}, now=1.0)
        _, soon = dstep(FAILING_HOUR, state, now=1.0 + monitor.REALERT_AFTER_S - 1)
        assert soon is None
        _, later = dstep(FAILING_HOUR, state, now=1.0 + monitor.REALERT_AFTER_S + 1)
        assert later is not None
        _, recovered = dstep(QUIET_HOUR, state, now=2.0)
        assert recovered is not None and recovered.startswith("RECOVERED")

    def test_an_unreadable_reading_changes_nothing_and_says_nothing(self):
        # The scheduling axis already says when the API cannot be reached.
        state, _ = dstep(FAILING_HOUR, {}, now=1.0)
        after, msg = dstep(None, state, now=2.0)
        assert msg is None and after == state

    def test_only_a_whole_reading_is_read(self):
        good = json.dumps(FAILING_HOUR)
        assert monitor.read_delivery(200, good) == FAILING_HOUR
        for status in (503, 404):
            assert monitor.read_delivery(status, good) is None
        assert monitor.read_delivery(200, "not json") is None
        no_classes = {k: v for k, v in FAILING_HOUR.items() if k != "by_class"}
        assert monitor.read_delivery(200, json.dumps(no_classes)) is None
        # Each count on its own: missing, a bool, negative, or not a number.
        for key in ("sent_in_window", "failed_or_ambiguous"):
            missing = {k: v for k, v in FAILING_HOUR.items() if k != key}
            assert monitor.read_delivery(200, json.dumps(missing)) is None, key
            for count in (True, -1, "3"):
                bad = {**FAILING_HOUR, key: count}
                assert monitor.read_delivery(200, json.dumps(bad)) is None, bad

    def test_the_delivery_surface_sits_beside_the_scheduling_one(self):
        assert (
            monitor.delivery_url("https://api.example/health/scheduling")
            == "https://api.example/health/delivery"
        )
        assert monitor.delivery_url("https://api.example/elsewhere") is None

    def test_main_reads_delivery_beside_scheduling_and_pages_it_alone(
        self, monkeypatch, tmp_path
    ):
        """`main` fetches the delivery surface beside --url and sends its page
        as its own message, recorded on the delivery axis alone."""
        sent = []
        delivery = (200, json.dumps(FAILING_HOUR))
        code = run_main(
            monkeypatch, tmp_path, 200, body(0, 5, 3), delivery=delivery, calls=sent
        )
        assert code == monitor.EXIT_SPOKE
        assert len(sent) == 1 and "DELIVERY IS FAILING" in sent[0]
        st = monitor.load_state(str(tmp_path / "s.json"))
        assert st["delivery"]["announced"] == monitor.DELIVERY_FAILING
        assert st.get("announced") is None

    def test_delivery_url_names_the_surface_read(self, monkeypatch, tmp_path):
        """`--delivery-url` replaces the surface beside --url, which then
        answers nothing."""
        sent = []
        code = run_main(
            monkeypatch,
            tmp_path,
            200,
            body(0, 5, 3),
            delivery=(200, json.dumps(FAILING_HOUR)),
            delivery_url="http://y/delivery",
            calls=sent,
            extra=("--delivery-url", "http://y/delivery"),
        )
        assert code == monitor.EXIT_SPOKE
        assert len(sent) == 1 and "DELIVERY IS FAILING" in sent[0]


def with_commits(api="abc1234", worker="abc1234"):
    """A healthy scheduling reading carrying the API's and the worker's short
    commits. `None` leaves that commit out, as a pre-rollout reading does."""
    worker_block = dict(ALIVE_WORKER)
    if worker is not None:
        worker_block["commit"] = worker
    data = {"stalled": 0, "accounts_active": 5, "max_lag_seconds": 3}
    data["worker"] = worker_block
    if api is not None:
        data["api_commit"] = api
    return data


def sstep(payload, prior, now):
    """`step` on the split's own state."""
    return step(payload, prior, now, decide=monitor.decide_split)


class TestAVersionSplitPagesOnceItOutlastsADeploy:
    """The API and the worker deploy one after the other, so they disagree for
    a deploy's length. A split that outlasts that is a deploy that reached one
    service and not the other."""

    def test_matching_commits_say_nothing(self):
        _, msg = sstep(with_commits(), {}, now=1.0)
        assert msg is None

    def test_a_split_is_silent_inside_the_window_then_pages(self):
        split = with_commits(worker="def5678")
        state, msg = sstep(split, {}, now=1.0)
        assert msg is None
        state, msg = sstep(split, state, now=1.0 + monitor.SPLIT_AFTER_S - 1)
        assert msg is None
        _, msg = sstep(split, state, now=1.0 + monitor.SPLIT_AFTER_S)
        assert msg is not None and "VERSION SPLIT" in msg
        assert "the API runs abc1234, the worker def5678" in msg

    def test_it_waits_half_an_hour_and_repeats_after_a_day(self):
        """The two values the operating guide states, as numbers: quiet at 29
        minutes, a page at 30, quiet for the next 23 hours, a page after 24."""
        split = with_commits(worker="def5678")
        state, _ = sstep(split, {}, now=0.0)
        _, early = sstep(split, state, now=29 * 60)
        assert early is None
        state, first = sstep(split, state, now=30 * 60)
        assert first is not None
        _, soon = sstep(split, state, now=30 * 60 + 23 * 3600)
        assert soon is None
        _, later = sstep(split, state, now=30 * 60 + 24 * 3600)
        assert later is not None

    def test_it_repeats_daily_and_says_when_it_is_resolved(self):
        split, t = with_commits(worker="def5678"), monitor.SPLIT_AFTER_S
        state, _ = sstep(split, {}, now=0.0)
        state, first = sstep(split, state, now=t)
        assert first is not None
        _, soon = sstep(split, state, now=t + monitor.RENOTICE_SPLIT_AFTER_S - 1)
        assert soon is None
        _, later = sstep(split, state, now=t + monitor.RENOTICE_SPLIT_AFTER_S + 1)
        assert later is not None
        _, resolved = sstep(with_commits(), state, now=t + 60)
        assert resolved is not None and "RESOLVED" in resolved

    def test_a_split_that_ends_inside_the_window_is_never_said(self):
        state, _ = sstep(with_commits(worker="def5678"), {}, now=0.0)
        _, msg = sstep(with_commits(), state, now=60.0)
        assert msg is None

    def test_a_reading_without_both_commits_neither_resets_nor_speaks(self):
        split = with_commits(worker="def5678")
        state, _ = sstep(split, {}, now=0.0)
        for unknown in ({}, with_commits(worker=None), with_commits(api=None)):
            kept, msg = sstep(unknown, state, now=60.0)
            assert msg is None and kept == state
        _, paged = sstep(split, state, now=monitor.SPLIT_AFTER_S)
        assert paged is not None, "the window counts from the first split reading"

    def test_main_reads_the_split_from_the_scheduling_reading(
        self, monkeypatch, tmp_path
    ):
        """`main` takes both commits from the one scheduling reading, keeps the
        split's history under "split", and records that axis alone as told.
        The first poll opens the window; back-dating its start stands in for
        the half hour."""
        sent = []
        raw = json.dumps(with_commits(worker="def5678"))
        assert run_main(monkeypatch, tmp_path, 200, raw, calls=sent) == (
            monitor.EXIT_QUIET
        )
        path = str(tmp_path / "s.json")
        st = monitor.load_state(path)
        assert st["split"]["state"] == monitor.SPLIT
        st["split"]["since"] -= monitor.SPLIT_AFTER_S
        monitor.save_state(path, st)

        assert run_main(monkeypatch, tmp_path, 200, raw, calls=sent) == (
            monitor.EXIT_SPOKE
        )
        assert len(sent) == 1 and "VERSION SPLIT" in sent[0]
        st = monitor.load_state(path)
        assert st["split"]["announced"] == monitor.SPLIT
        assert st.get("announced") is None


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

    def test_no_signal_speaks_once_then_is_quiet(self, monkeypatch, tmp_path):
        sent = []
        assert (
            run_main(monkeypatch, tmp_path, 200, body(active=0), calls=sent)
            == monitor.EXIT_SPOKE
        )
        assert (
            run_main(monkeypatch, tmp_path, 200, body(active=0), calls=sent)
            == monitor.EXIT_QUIET
        )
        assert len(sent) == 1

    def test_a_stall_reaches_the_notifier(self, monkeypatch, tmp_path):
        sent = []
        assert (
            run_main(monkeypatch, tmp_path, 200, body(3, 9, 4000), calls=sent)
            == monitor.EXIT_SPOKE
        )
        assert "FLEET ALERT" in sent[0]

    def test_a_failed_notify_exits_nonzero_and_does_not_record_speaking(
        self, monkeypatch, tmp_path
    ):
        # A monitor cannot page about its own paging failure. What it CAN do is
        # refuse to believe it spoke, so the next poll tries again instead of
        # de-duplicating against a message nobody received.
        rc = run_main(monkeypatch, tmp_path, 200, body(active=0), notify_ok=False)
        assert rc == monitor.EXIT_NOTIFY_FAILED
        st = monitor.load_state(str(tmp_path / "s.json"))
        assert st["spoke_at"] == 0.0
        assert "notify_failed_at" in st
        assert (
            run_main(monkeypatch, tmp_path, 200, body(active=0)) == monitor.EXIT_SPOKE
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
            run_main(monkeypatch, tmp_path, 200, body(2, 5, 4000), calls=sent)
            == monitor.EXIT_SPOKE
        )
        assert "FLEET ALERT" in sent[0]

        rc = run_main(monkeypatch, tmp_path, 200, body(0, 5, 2), notify_ok=False)
        assert rc == monitor.EXIT_NOTIFY_FAILED
        assert monitor.load_state(str(tmp_path / "s.json"))["announced"] == STALLED

        assert (
            run_main(monkeypatch, tmp_path, 200, body(0, 5, 2), calls=sent)
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

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param(body(stalled=0, active=1, lag=0, worker=None), id="no-lag"),
            pytest.param(
                body(stalled=2, active=5, lag=5000, worker=None), id="a-stall"
            ),
        ],
    )
    def test_a_missing_worker_block_is_unreachable_and_pages_on_the_second_poll(
        self, monkeypatch, tmp_path, capsys, raw
    ):
        """A reading with no worker block is a broken instrument, and `main`
        must treat it as one.

        With a destination active and no lag, the cursor axis alone would answer
        `healthy` and the worker axis would be gone without a word. With a lag
        past the threshold it would answer `stalled`, and the instrument still
        comes first. So the first poll prints `unreachable` and stays quiet
        (one reading is not enough), the second pages, and neither prints
        `healthy`.
        """
        alert = "FLEET ALERT: storydump scheduling check UNREACHABLE"
        sent = []

        rc = run_main(monkeypatch, tmp_path, 200, raw, calls=sent)
        out = capsys.readouterr().out
        assert rc == monitor.EXIT_QUIET
        assert any(line.startswith("unreachable: ") for line in out.splitlines())
        assert "healthy" not in out.lower()
        assert sent == []

        rc = run_main(monkeypatch, tmp_path, 200, raw, calls=sent)
        out = capsys.readouterr().out
        assert rc == monitor.EXIT_SPOKE
        assert "healthy" not in out.lower()
        assert len(sent) == 1
        assert sent[0].startswith(alert)


# ── the worker-liveness axis ─────────────────────────────────────────────────
#
# `NO_SIGNAL` is the honest answer for the CURSOR axis on an empty estate, and
# it is also the answer a DEAD WORKER produces there. This pins that an outage
# pages at once; blindness (`WORKER_UNKNOWN`) is said but never pages, and is
# pinned with `NO_SIGNAL` in `TestNothingToSeeIsNotTheSameAsNothingWrong`.


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


# ── the threshold flags ──────────────────────────────────────────────────────
#
# The classifier decides what a reading means; the poller only hands it the
# thresholds. These tests pin that the hand-off is real: the defaults are the
# classifier's own, and each flag reaches `classify`.


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


@pytest.mark.parametrize(
    "flag,value,raw,said",
    [
        pytest.param(
            "--stall-threshold",
            DEFAULT_STALL_THRESHOLD_S // 4,
            body(1, 5, DEFAULT_STALL_THRESHOLD_S // 2),
            "SCHEDULING IS STALLED",
            id="stall",
        ),
        pytest.param(
            "--worker-stale-threshold",
            DEFAULT_WORKER_STALE_S // 4,
            body(
                0,
                5,
                0,
                worker={
                    **ALIVE_WORKER,
                    "last_success_age_seconds": DEFAULT_WORKER_STALE_S // 2,
                },
            ),
            "no system job has finished",
            id="worker-stale",
        ),
        pytest.param(
            "--worker-overdue-threshold",
            DEFAULT_WORKER_OVERDUE_S // 4,
            body(
                0,
                5,
                0,
                worker={
                    **ALIVE_WORKER,
                    "overdue_ready": 2,
                    "max_overdue_seconds": DEFAULT_WORKER_OVERDUE_S // 2,
                },
            ),
            "due system job(s) unclaimed",
            id="worker-overdue",
        ),
    ],
)
def test_each_threshold_flag_is_reachable_from_the_command_line(
    monkeypatch, tmp_path, flag, value, raw, said
):
    """Adding a flag and forgetting to thread it into `classify` is the
    ordinary way this breaks: the defaults keep every test green while the
    operator's setting silently does nothing.

    So each case drives `main` with a reading that is healthy under the
    DEFAULTS and a fault only under its flag. The numbers are derived from the
    defaults, so both halves hold whatever they become. It can only pass if
    the value travelled.
    """
    sent = []
    assert classify(200, raw, threshold_s=DEFAULT_STALL_THRESHOLD_S).state == HEALTHY

    rc = run_main(monkeypatch, tmp_path, 200, raw, calls=sent, extra=(flag, str(value)))

    assert rc == monitor.EXIT_SPOKE, "the flag did not reach classify"
    assert sent and said in sent[0] and f"(threshold {value}s)" in sent[0]


# ── pins: these pass on main by design ──────────────────────────────────────
#
# Each one fixes a value or a transition a mutant could change while every
# test above stays green.


def test_the_exit_codes_are_the_ones_a_supervisor_routes_on():
    assert (monitor.EXIT_QUIET, monitor.EXIT_SPOKE, monitor.EXIT_NOTIFY_FAILED) == (
        0,
        10,
        11,
    )


@pytest.mark.parametrize(
    "fault,readings",
    [
        pytest.param(
            lambda: classify(200, body(2, 5, 5000), threshold_s=T), 1, id="stalled"
        ),
        pytest.param(
            lambda: Verdict(WORKER_DOWN, "no system job has finished for 90000s"),
            1,
            id="worker-down",
        ),
        pytest.param(lambda: classify(0, "boom", threshold_s=T), 2, id="unreachable"),
    ],
)
def test_every_fault_repeats_while_it_lasts_and_announces_recovery(fault, readings):
    """Not only the stall: each fault pages, repeats on the fault cadence, and
    ends in RECOVERED, so its alerts stopping never reads as the monitor dying."""
    v, state, msg = fault(), {}, None
    for i in range(readings):
        state, msg = step(v, state, now=1000.0 + i)
    assert msg is not None and "FLEET ALERT" in msg
    t = 1000.0 + readings - 1
    _, soon = step(v, state, now=t + monitor.REALERT_AFTER_S - 1)
    assert soon is None
    _, later = step(v, state, now=t + monitor.REALERT_AFTER_S + 1)
    assert later is not None
    _, recovered = step(classify(200, body(0, 5, 3), threshold_s=T), state, now=t + 60)
    assert recovered is not None and "RECOVERED" in recovered
