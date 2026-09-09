"""The poster's decisions, which are pure so they can be tested rather than
staked out (#1268).

The load-bearing tests here are the ones proving **`never-posted` expires**.
The sibling's `no-signal` never alerts, and on the cursor axis that is right —
an estate with no destinations is a legitimate pre-launch state. On the posting
axis the identical shape IS the outage: 0 `ig_accounts`, 0 `workspaces`, 0
`media_sources`, nothing that could mint, for nine days, with the scheduling
monitor reporting `healthy` throughout.

So the two tests this file exists for are `TestNoPostYetIsOnADeadline` and
`test_an_empty_estate_still_alerts`. Everything else is ordinary.
"""

from __future__ import annotations

import json

import pytest

from scripts.posting_monitor import (
    DEFAULT_GRACE_S,
    DEFAULT_SILENCE_THRESHOLD_S,
    EXIT_NOTIFY_FAILED,
    EXIT_QUIET,
    EXIT_SPOKE,
    NEVER_POSTED,
    NEVER_POSTED_OVERDUE,
    POSTING,
    REALERT_AFTER_S,
    RENOTICE_AFTER_S,
    SILENT,
    UNREACHABLE,
    Verdict,
    announce,
    classify,
    decide,
    load_state,
    save_state,
)

HOUR = 3600
SILENCE = DEFAULT_SILENCE_THRESHOLD_S
GRACE = DEFAULT_GRACE_S


def payload(**over):
    """A healthy estate, overridden per test.

    Spelled as a full payload rather than a partial, because `classify` is
    strict about every key and a fixture that omitted one would exercise the
    strictness path while claiming to test something else.
    """
    base = {
        "posted_ever": 412,
        "last_post_age_seconds": 4 * HOUR,
        "intents_ever": 500,
        "oldest_intent_age_seconds": 90 * 24 * HOUR,
        "oldest_active_destination_age_seconds": 120 * 24 * HOUR,
        "debited_total": 412,
        "ledger_days": 140,
        "accounts_active": 2,
    }
    base.update(over)
    return json.dumps(base)


def read(body, *, watched_s=10 * 24 * HOUR, silence_s=SILENCE, grace_s=GRACE):
    return classify(
        200, body, silence_s=silence_s, grace_s=grace_s, watched_s=watched_s
    )


def step(verdict, prior, now):
    """Exactly what `main` does: decide, then record the announcement ONLY if a
    message actually went out.

    Mirrored here rather than hand-rolled, because the two-field split
    (`state` = what the endpoint said, `announced` = what a human was told) is
    the thing under test. A test that advanced `announced` unconditionally would
    pass against the very bug that split fixes.
    """
    state, msg = decide(verdict, prior, now)
    if msg is not None:
        state = announce(state, verdict, now)
    return state, msg


# --------------------------------------------------------------------------
# The reason this file exists.
# --------------------------------------------------------------------------


class TestNoPostYetIsOnADeadline:
    """`never-posted` must EXPIRE into an alert. The sibling's `no-signal` does
    not, and a permanent one here would have excused all sixteen days."""

    #: No landings AND no database-side ladder rung, so each test below opts
    #: exactly one anchor back in and nothing else can trip the grace for it.
    NEVER = dict(
        posted_ever=0,
        last_post_age_seconds=None,
        oldest_active_destination_age_seconds=None,
        debited_total=0,
        ledger_days=0,
    )

    def test_inside_the_grace_it_is_a_notice(self):
        v = read(
            payload(**self.NEVER, intents_ever=0, oldest_intent_age_seconds=None),
            watched_s=GRACE - HOUR,
        )
        assert v.state == NEVER_POSTED
        _, msg = decide(v, {}, 1000.0)
        assert "not an alert and not an all-clear" in msg
        assert "FLEET ALERT" not in msg

    def test_past_the_grace_the_SAME_reading_becomes_an_alert(self):
        """One payload, two ages of the monitor. Nothing about the estate
        changed — which is the point: the clock is what makes it speak."""
        body = payload(**self.NEVER, intents_ever=0, oldest_intent_age_seconds=None)
        assert read(body, watched_s=GRACE - HOUR).state == NEVER_POSTED
        assert read(body, watched_s=GRACE + HOUR).state == NEVER_POSTED_OVERDUE

    def test_the_expiry_boundary_is_inclusive_and_does_not_straddle(self):
        body = payload(**self.NEVER, intents_ever=0, oldest_intent_age_seconds=None)
        assert read(body, watched_s=GRACE - 1).state == NEVER_POSTED
        assert read(body, watched_s=GRACE).state == NEVER_POSTED_OVERDUE

    def test_the_overdue_alert_pages_on_the_very_first_reading(self):
        """Like `silent` and unlike `unreachable`. The elapsed grace already
        CONTAINS its duration; waiting a poll to confirm only lengthens the
        outage."""
        v = read(payload(**self.NEVER), watched_s=GRACE + HOUR)
        state, msg = step(v, {}, 1000.0)
        assert state["consecutive"] == 1
        assert msg.startswith("FLEET ALERT")
        assert "HAS NEVER POSTED" in msg

    def test_the_oldest_intent_trips_the_grace_on_a_FRESH_state_file(self):
        """The second anchor. A monitor deployed today onto an estate that has
        held intents for a week must not restart the clock — the state file is
        off-host and the app cannot reach it, but it can be LOST, and a lost
        clock is the one direction a monitor must not fail in quietly."""
        v = read(
            payload(**self.NEVER, oldest_intent_age_seconds=GRACE + HOUR),
            watched_s=0.0,
        )
        assert v.state == NEVER_POSTED_OVERDUE
        assert "the oldest intent" in v.detail

    def test_the_anchor_used_is_named_in_the_message(self):
        """Which clock spoke changes what a human should check, so the message
        says which one it was."""
        assert (
            "this monitor started watching"
            in read(
                payload(**self.NEVER, oldest_intent_age_seconds=HOUR),
                watched_s=GRACE + HOUR,
            ).detail
        )

    def test_a_connected_destination_trips_the_grace_before_any_intent_exists(
        self,
    ):
        """The rung the intent anchor cannot reach.

        An estate can hold live destinations for days before minting a single
        intent — production did, 2026-09-02 to 09-06. Inside that window
        `oldest_intent_age_seconds` is null, so a monitor installed there had no
        database-side anchor at all and would sit on a NOTICE for 72h with
        destinations idle for six days.
        """
        v = read(
            payload(
                **{**self.NEVER, "oldest_active_destination_age_seconds": None},
                intents_ever=0,
                oldest_intent_age_seconds=None,
                accounts_active=2,
            ),
            watched_s=0.0,
        )
        assert v.state == NEVER_POSTED, "no rung yet — this must still be a notice"

        v = read(
            payload(
                **{
                    **self.NEVER,
                    "oldest_active_destination_age_seconds": GRACE + HOUR,
                },
                intents_ever=0,
                oldest_intent_age_seconds=None,
                accounts_active=2,
            ),
            watched_s=0.0,
        )
        assert v.state == NEVER_POSTED_OVERDUE
        assert "the oldest active destination was connected" in v.detail

    def test_the_latest_rung_wins_and_ties_go_to_the_local_clock(self):
        """`max` over the ladder, keyed on the SECONDS.

        The tie half of this does NOT kill the `key=` mutant, and that is worth
        saying rather than leaving for someone to rediscover: whole-tuple
        comparison reaches the name only on an exact tie, and these three names
        sort so that the local clock is both first in the list and largest
        alphabetically. The mutant is inert, not untested. What this pins is the
        documented behaviour — latest rung wins, ties go to the local clock.
        """
        v = read(
            payload(
                **{
                    **self.NEVER,
                    "oldest_active_destination_age_seconds": GRACE + HOUR,
                },
                oldest_intent_age_seconds=GRACE + 10 * HOUR,
            ),
            watched_s=GRACE + 5 * HOUR,
        )
        assert "the oldest intent" in v.detail

        v = read(payload(**self.NEVER, oldest_intent_age_seconds=None), watched_s=0.0)
        assert "this monitor started watching" in v.detail

    def test_a_young_intent_cannot_SHORTEN_the_watch_clock(self):
        """`max`, not `min`. The second anchor may only make this speak sooner;
        an anchor that could pull the deadline in would hand the app a way to
        suppress the alert by minting a fresh intent."""
        v = read(
            payload(**self.NEVER, oldest_intent_age_seconds=60),
            watched_s=GRACE + HOUR,
        )
        assert v.state == NEVER_POSTED_OVERDUE


def test_an_empty_estate_still_alerts():
    """THE ANTI-REGRESSION.

    Phase (a) of the 2026 outage: 0 destinations, 0 intents, nothing that could
    mint. The tempting design gates the alert on `accounts_active > 0` —
    "nothing is expected to post, so stay quiet" — which is true, useless, and
    excuses this state forever. It is the gauge this instrument replaces,
    rebuilt inside the replacement.
    """
    v = read(
        payload(
            posted_ever=0,
            last_post_age_seconds=None,
            intents_ever=0,
            oldest_intent_age_seconds=None,
            oldest_active_destination_age_seconds=None,
            debited_total=0,
            ledger_days=0,
            accounts_active=0,
        ),
        watched_s=16 * 24 * HOUR,
    )
    assert v.state == NEVER_POSTED_OVERDUE
    assert "nothing could mint a post" in v.detail


def test_the_destination_count_never_reaches_the_verdict():
    """`accounts_active` is alert TEXT and nothing else. Two readings differing
    only in it must classify identically."""
    never = dict(
        posted_ever=0,
        last_post_age_seconds=None,
        intents_ever=6,
        oldest_intent_age_seconds=2 * 24 * HOUR,
        oldest_active_destination_age_seconds=None,
        debited_total=0,
        ledger_days=0,
    )
    empty = read(payload(**never, accounts_active=0), watched_s=GRACE + HOUR)
    full = read(payload(**never, accounts_active=5), watched_s=GRACE + HOUR)
    assert empty.state == full.state == NEVER_POSTED_OVERDUE


class TestBothPhasesOfTheOutageAreFalse:
    """#1268's acceptance condition, stated as a test.

    Two stacked causes, opposite remedies, and `/health/scheduling` published
    `healthy` across both — 1936 consecutive readings, every field of them true.
    The one assertion false in both is *a post landed*.
    """

    #: 08-24 → ~09-02. Empty tier: nothing could mint.
    PHASE_A = dict(
        posted_ever=0,
        last_post_age_seconds=None,
        intents_ever=0,
        oldest_intent_age_seconds=None,
        oldest_active_destination_age_seconds=None,
        debited_total=0,
        ledger_days=0,
        accounts_active=0,
    )

    #: 09-07 → onward. Populated and minting; six intents stranded
    #: `awaiting_approval`, which is a NOMINAL state for every field the
    #: scheduling monitor publishes.
    #: The destinations predate the intents — provisioned ~09-02, first intent
    #: 09-07. That gap is exactly the window the destination rung covers.
    PHASE_B = dict(
        posted_ever=0,
        last_post_age_seconds=None,
        intents_ever=6,
        oldest_intent_age_seconds=2 * 24 * HOUR,
        oldest_active_destination_age_seconds=7 * 24 * HOUR,
        debited_total=0,
        ledger_days=0,
        accounts_active=2,
    )

    @pytest.mark.parametrize("phase", [PHASE_A, PHASE_B], ids=["empty", "stranded"])
    def test_neither_phase_ever_reads_as_posting(self, phase):
        for watched in (0.0, HOUR, GRACE - 1, GRACE, 16 * 24 * HOUR):
            assert read(payload(**phase), watched_s=watched).state != POSTING

    @pytest.mark.parametrize("phase", [PHASE_A, PHASE_B], ids=["empty", "stranded"])
    def test_both_phases_alert_once_the_monitor_has_watched_long_enough(self, phase):
        v = read(payload(**phase), watched_s=16 * 24 * HOUR)
        assert v.state == NEVER_POSTED_OVERDUE
        _, msg = decide(v, {}, 1000.0)
        assert msg.startswith("FLEET ALERT")

    def test_the_two_phases_are_told_apart_in_the_alert_text(self):
        """Same verdict, opposite remedies — connect a destination, or unblock
        approvals. An alert that could not distinguish them would be correct and
        useless at 3am."""
        a = read(payload(**self.PHASE_A), watched_s=16 * 24 * HOUR).detail
        b = read(payload(**self.PHASE_B), watched_s=16 * 24 * HOUR).detail
        assert "nothing could mint a post" in a
        assert "stuck before publishing" in b
        assert a != b


def test_debits_without_landings_are_called_out_as_such():
    """The cap ledger #1268 names, earning its place in the CONTRAST.

    `daily_post_counts` is debited at the `approved → publishing` flip, BEFORE
    the publish call, so a nonzero total beside zero landings means publishing
    is being attempted and failing every time — a sharper diagnosis than either
    number gives alone, and the reason the verdict does not rest on that table.
    """
    v = read(
        payload(
            posted_ever=0,
            last_post_age_seconds=None,
            intents_ever=40,
            oldest_intent_age_seconds=10 * 24 * HOUR,
            debited_total=37,
            ledger_days=9,
        ),
        watched_s=GRACE + HOUR,
    )
    assert v.state == NEVER_POSTED_OVERDUE
    assert "publishing is being ATTEMPTED and nothing is landing" in v.detail


# --------------------------------------------------------------------------
# Ordinary properties.
# --------------------------------------------------------------------------


class TestTheHealthyAndSilentSplit:
    def test_a_recent_post_is_healthy_and_silent_on_the_wire(self):
        v = read(payload())
        assert v.state == POSTING
        assert decide(v, {}, 1000.0)[1] is None

    def test_past_the_threshold_it_is_an_outage(self):
        v = read(payload(last_post_age_seconds=SILENCE + 1))
        assert v.state == SILENT
        assert "HAS STOPPED POSTING" in decide(v, {}, 1000.0)[1]

    def test_the_threshold_boundary(self):
        assert read(payload(last_post_age_seconds=SILENCE)).state == POSTING
        assert read(payload(last_post_age_seconds=SILENCE + 1)).state == SILENT

    def test_the_alert_says_the_scheduling_monitor_cannot_see_this(self):
        """The sentence a reader needs most: the gauge they trust is not
        broken, it is blind to this axis, so its green light is not a
        contradiction to go looking into."""
        v = read(payload(last_post_age_seconds=SILENCE + 1))
        assert "reads the clock and the worker" in decide(v, {}, 1000.0)[1]


class TestAMalformedAnswerIsNeverAHealthyOne:
    @pytest.mark.parametrize(
        "key",
        [
            "posted_ever",
            "intents_ever",
            "debited_total",
            "ledger_days",
            "accounts_active",
        ],
    )
    def test_a_missing_count_is_unreachable(self, key):
        body = json.loads(payload())
        del body[key]
        assert read(json.dumps(body)).state == UNREACHABLE

    @pytest.mark.parametrize(
        "key, null_is_legal_when",
        [
            # A null age contradicts a nonzero count, so the surrounding
            # payload has to make the null legal or this would exercise the
            # contradiction path while claiming to test the sentinel.
            ("last_post_age_seconds", {"posted_ever": 0}),
            ("oldest_intent_age_seconds", {}),
            ("oldest_active_destination_age_seconds", {}),
        ],
    )
    def test_a_missing_age_is_unreachable_even_though_NULL_is_legal(
        self, key, null_is_legal_when
    ):
        """The sentinel's whole job. `null` means "there has never been one";
        an absent key means the endpoint is not serving the field, and reading
        the second as the first would invent a fact."""
        body = json.loads(payload(**null_is_legal_when))
        del body[key]
        assert read(json.dumps(body)).state == UNREACHABLE
        body[key] = None
        assert read(json.dumps(body)).state != UNREACHABLE

    def test_a_boolean_is_not_a_count(self):
        assert read(payload(posted_ever=True)).state == UNREACHABLE

    @pytest.mark.parametrize(
        "body", ["not json", "[]", '"a string"', "null"], ids=range(4)
    )
    def test_a_non_object_body_is_unreachable(self, body):
        assert read(body).state == UNREACHABLE

    def test_a_non_200_is_unreachable(self):
        assert (
            classify(503, "", silence_s=SILENCE, grace_s=GRACE, watched_s=0).state
            == UNREACHABLE
        )

    def test_a_transport_failure_names_itself(self):
        v = classify(
            0,
            "URLError: <urlopen error [Errno -2] Name or service not known>",
            silence_s=SILENCE,
            grace_s=GRACE,
            watched_s=0,
        )
        assert v.state == UNREACHABLE
        assert "Name or service not known" in v.detail


class TestTheCountAndTheAgeMustAgree:
    """One fact from two directions, so they can contradict — and a
    contradiction is an instrument fault neither field reveals alone."""

    def test_posts_recorded_but_no_age_is_unreachable(self):
        assert (
            read(payload(posted_ever=7, last_post_age_seconds=None)).state
            == UNREACHABLE
        )

    def test_an_age_with_no_posts_recorded_is_unreachable(self):
        assert (
            read(payload(posted_ever=0, last_post_age_seconds=100)).state == UNREACHABLE
        )

    def test_it_is_not_resolved_toward_the_reassuring_reading(self):
        """A contradiction resolved toward `posting` would be the fail-toward-
        good-news class this whole instrument exists for."""
        for v in (
            read(payload(posted_ever=7, last_post_age_seconds=None)),
            read(payload(posted_ever=0, last_post_age_seconds=100)),
        ):
            assert v.state != POSTING


class TestConfirmationIsAsymmetricBecauseTheSignalsAre:
    def test_silent_pages_on_the_first_reading(self):
        _, msg = step(read(payload(last_post_age_seconds=SILENCE + 1)), {}, 1000.0)
        assert msg is not None

    def test_unreachable_waits_for_the_second(self):
        v = Verdict(UNREACHABLE, "HTTP 502")
        state, msg = step(v, {}, 1000.0)
        assert msg is None and state["consecutive"] == 1
        state, msg = step(v, state, 2000.0)
        assert msg is not None and state["consecutive"] == 2

    def test_unreachable_says_it_cannot_look_rather_than_that_all_is_well(self):
        state, _ = step(Verdict(UNREACHABLE, "HTTP 502"), {}, 1000.0)
        _, msg = step(Verdict(UNREACHABLE, "HTTP 502"), state, 2000.0)
        assert "not the same as posting being fine" in msg


class TestItRepeatsWhileBrokenAndAnnouncesRecovery:
    def test_a_standing_outage_repeats_rather_than_going_quiet(self):
        v = read(payload(last_post_age_seconds=SILENCE + 1))
        state, first = step(v, {}, 1000.0)
        assert first is not None
        _, soon = step(v, state, 1000.0 + REALERT_AFTER_S - 1)
        assert soon is None
        _, later = step(v, state, 1000.0 + REALERT_AFTER_S)
        assert later is not None

    def test_the_pre_grace_notice_repeats_far_more_slowly(self):
        v = read(
            payload(
                posted_ever=0,
                last_post_age_seconds=None,
                oldest_intent_age_seconds=None,
                oldest_active_destination_age_seconds=None,
            ),
            watched_s=HOUR,
        )
        assert v.state == NEVER_POSTED
        state, _ = step(v, {}, 1000.0)
        assert step(v, state, 1000.0 + REALERT_AFTER_S)[1] is None
        assert step(v, state, 1000.0 + RENOTICE_AFTER_S)[1] is not None

    @pytest.mark.parametrize("prior_state", [SILENT, UNREACHABLE, NEVER_POSTED_OVERDUE])
    def test_recovery_is_announced_from_every_alerting_state(self, prior_state):
        _, msg = decide(read(payload()), {"announced": prior_state}, 2000.0)
        assert msg.startswith("RECOVERED")

    def test_the_first_post_ever_is_announced_rather_than_silently_healthy(self):
        _, msg = decide(read(payload()), {"announced": NEVER_POSTED}, 2000.0)
        assert "FIRST POST OBSERVED" in msg

    def test_a_steady_healthy_estate_says_nothing(self):
        assert decide(read(payload()), {"announced": POSTING}, 2000.0)[1] is None

    def test_a_failed_notify_does_not_count_as_having_spoken(self):
        """`announced` moves only in `announce`, which the caller runs only
        after delivery. With one field, a failed notify advances the history and
        the next poll stays quiet about a message nobody received."""
        v = read(payload(last_post_age_seconds=SILENCE + 1))
        state, msg = decide(v, {}, 1000.0)  # deliberately NOT announced
        assert msg is not None
        assert decide(v, state, 1000.0 + 60)[1] is not None


class TestTheStateFile:
    def test_first_seen_at_is_seeded_once_and_then_carried(self):
        """That field IS the grace clock. Re-seeding it on any path would hand
        the app a way to reset a deadline it must not be able to reach."""
        state, _ = decide(read(payload()), {}, 1000.0)
        assert state["first_seen_at"] == 1000.0
        later, _ = decide(read(payload()), state, 9999.0)
        assert later["first_seen_at"] == 1000.0

    def test_first_seen_at_survives_a_state_change(self):
        state, _ = decide(read(payload()), {}, 1000.0)
        state, _ = decide(Verdict(UNREACHABLE, "HTTP 502"), state, 2000.0)
        state, _ = decide(read(payload()), state, 3000.0)
        assert state["first_seen_at"] == 1000.0

    def test_a_missing_or_corrupt_file_reads_as_no_history(self, tmp_path):
        assert load_state(str(tmp_path / "absent.json")) == {}
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        assert load_state(str(bad)) == {}
        wrong = tmp_path / "wrong.json"
        wrong.write_text("[]", encoding="utf-8")
        assert load_state(str(wrong)) == {}

    def test_save_creates_the_directory_and_round_trips(self, tmp_path):
        path = str(tmp_path / "nested" / "deep" / "state.json")
        save_state(path, {"state": POSTING, "first_seen_at": 12.0})
        assert load_state(path)["first_seen_at"] == 12.0

    def test_save_leaves_no_temporary_files_behind(self, tmp_path):
        path = str(tmp_path / "state.json")
        save_state(path, {"state": POSTING})
        assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


class TestMainEndToEnd:
    """The wiring, driven through `main` with the network and the pager stubbed
    — the thresholds reaching `classify`, and the exit codes a supervisor routes
    on."""

    @staticmethod
    def _run(monkeypatch, tmp_path, body, argv_extra=(), status=200, notify_ok=True):
        import scripts.posting_monitor as mod

        sent = []
        monkeypatch.setattr(mod, "fetch", lambda url, t: (status, body))
        monkeypatch.setattr(
            mod, "notify", lambda cmd, msg: (sent.append(msg), notify_ok)[1]
        )
        state_file = str(tmp_path / "state.json")
        rc = mod.main(
            [
                "--url",
                "http://example.invalid/health/posting",
                "--state-file",
                state_file,
                "--notify-command",
                "/bin/true",
                *argv_extra,
            ]
        )
        return rc, sent, load_state(state_file)

    def test_a_healthy_estate_exits_quiet_and_pages_nobody(self, monkeypatch, tmp_path):
        rc, sent, state = self._run(monkeypatch, tmp_path, payload())
        assert rc == EXIT_QUIET and sent == []
        assert state["state"] == POSTING

    def test_an_outage_pages_and_exits_spoke(self, monkeypatch, tmp_path):
        rc, sent, state = self._run(
            monkeypatch, tmp_path, payload(last_post_age_seconds=SILENCE + 1)
        )
        assert rc == EXIT_SPOKE and len(sent) == 1
        assert state["announced"] == SILENT

    def test_a_failed_pager_exits_nonzero_and_leaves_announced_alone(
        self, monkeypatch, tmp_path
    ):
        rc, _, state = self._run(
            monkeypatch,
            tmp_path,
            payload(last_post_age_seconds=SILENCE + 1),
            notify_ok=False,
        )
        assert rc == EXIT_NOTIFY_FAILED
        assert state["announced"] is None
        assert "notify_failed_at" in state

    def test_the_thresholds_are_reachable_from_the_command_line(
        self, monkeypatch, tmp_path
    ):
        """A deployment that changes the posting cadence must be able to move
        these without editing the file."""
        body = payload(last_post_age_seconds=10 * HOUR)
        rc, _, state = self._run(monkeypatch, tmp_path, body)
        assert state["state"] == POSTING
        rc, sent, state = self._run(
            monkeypatch, tmp_path, body, argv_extra=["--silence-threshold", "3600"]
        )
        assert state["state"] == SILENT and len(sent) == 1

    def test_the_grace_is_reachable_from_the_command_line(self, monkeypatch, tmp_path):
        body = payload(
            posted_ever=0,
            last_post_age_seconds=None,
            oldest_intent_age_seconds=6 * HOUR,
            oldest_active_destination_age_seconds=6 * HOUR,
        )
        _, _, state = self._run(monkeypatch, tmp_path, body)
        assert state["state"] == NEVER_POSTED
        _, sent, state = self._run(
            monkeypatch, tmp_path, body, argv_extra=["--grace", "3600"]
        )
        assert state["state"] == NEVER_POSTED_OVERDUE and len(sent) == 1

    def test_the_watch_clock_advances_across_runs(self, monkeypatch, tmp_path):
        """The grace clock is the state file's, so it has to survive a poll.

        Driven through `_run` twice against ONE `tmp_path`, which is what makes
        the second call see the first's `first_seen_at`. Only the clock moves
        between them — same payload, same estate.
        """
        import scripts.posting_monitor as mod

        body = payload(
            posted_ever=0,
            last_post_age_seconds=None,
            intents_ever=0,
            oldest_intent_age_seconds=None,
            oldest_active_destination_age_seconds=None,
        )
        grace = ["--grace", "100"]

        # The first observation SPEAKS — a notice, not an alert. That is the
        # sibling's `no-signal` behaviour and it is right: "nothing has posted
        # yet" must be said out loud once rather than left to look like quiet.
        monkeypatch.setattr(mod.time, "time", lambda: 1000.0)
        rc, sent, state = self._run(monkeypatch, tmp_path, body, argv_extra=grace)
        assert rc == EXIT_SPOKE and state["state"] == NEVER_POSTED
        assert "NO POST YET" in sent[-1] and "FLEET ALERT" not in sent[-1]

        monkeypatch.setattr(mod.time, "time", lambda: 1000.0 + 101)
        rc, sent, state = self._run(monkeypatch, tmp_path, body, argv_extra=grace)
        assert rc == EXIT_SPOKE and state["state"] == NEVER_POSTED_OVERDUE
        assert sent[-1].startswith("FLEET ALERT")

    def test_status_prints_the_last_state_without_polling(
        self, monkeypatch, tmp_path, capsys
    ):
        import scripts.posting_monitor as mod

        state_file = str(tmp_path / "state.json")
        save_state(state_file, {"state": SILENT, "detail": "recorded earlier"})

        def _boom(*a, **k):  # pragma: no cover - must never run
            raise AssertionError("--status must not poll")

        monkeypatch.setattr(mod, "fetch", _boom)
        assert (
            mod.main(["--url", "u", "--state-file", state_file, "--status"])
            == EXIT_QUIET
        )
        assert json.loads(capsys.readouterr().out)["state"] == SILENT

    def test_the_verdict_is_always_printed_even_when_silent(
        self, monkeypatch, tmp_path, capsys
    ):
        """`never-posted` must never be something a reader has to go looking
        for."""
        self._run(
            monkeypatch, tmp_path, payload(posted_ever=0, last_post_age_seconds=None)
        )
        assert "never-posted" in capsys.readouterr().out


def test_a_fully_refunded_estate_is_not_misread_as_never_having_tried():
    """`debited_total` alone cannot see this, and the misdiagnosis it produces
    sends a reader to the wrong place.

    `publish_cap` refunds by decrementing the same counter, so an estate that
    claimed a publish slot every time and failed every time after the debit sums
    back to **zero** — identical to one that never reached publishing at all.
    The first needs the publish path looked at; the second needs approvals
    unblocked. `ledger_days` survives the refund and is what separates them.
    """
    never_tried = read(
        payload(
            posted_ever=0,
            last_post_age_seconds=None,
            intents_ever=6,
            oldest_intent_age_seconds=5 * 24 * HOUR,
            debited_total=0,
            ledger_days=0,
        ),
        watched_s=GRACE + HOUR,
    )
    all_refunded = read(
        payload(
            posted_ever=0,
            last_post_age_seconds=None,
            intents_ever=6,
            oldest_intent_age_seconds=5 * 24 * HOUR,
            debited_total=0,
            ledger_days=4,
        ),
        watched_s=GRACE + HOUR,
    )
    assert never_tried.state == all_refunded.state == NEVER_POSTED_OVERDUE
    assert "stuck before publishing" in never_tried.detail
    assert "failing after the cap debit" in all_refunded.detail
    assert never_tried.detail != all_refunded.detail


def test_a_silence_alert_does_not_contradict_itself():
    """`_describe_estate` runs on the `silent` path too, where `posted_ever > 0`.

    Its branches all diagnose an estate that has NEVER posted, so appending one
    to a silence alert produced "412 post(s) landed in total, and ... nothing is
    landing" — an alert arguing with itself, read by someone woken at 3am.
    """
    v = read(payload(posted_ever=412, last_post_age_seconds=SILENCE + 1))
    assert v.state == SILENT
    assert "nothing is landing" not in v.detail
    assert "stuck before publishing" not in v.detail
    # What it says instead: the arithmetic. Debits far above landings means
    # publishing is being reached and failing; level means nothing reaches it.
    assert "412 cap debit(s) against 412 landing(s)" in v.detail


def test_the_silence_alert_surfaces_attempts_that_outnumber_landings():
    v = read(
        payload(posted_ever=412, last_post_age_seconds=SILENCE + 1, debited_total=500)
    )
    assert v.state == SILENT
    assert "500 cap debit(s) against 412 landing(s)" in v.detail
