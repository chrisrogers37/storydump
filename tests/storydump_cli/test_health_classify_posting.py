from __future__ import annotations

import json

import pytest

from storydump_cli.health_classify.posting import (
    DEFAULT_GRACE_S,
    DEFAULT_SILENCE_THRESHOLD_S,
    NEVER_POSTED,
    NEVER_POSTED_OVERDUE,
    POSTING,
    SILENT,
    UNREACHABLE,
    classify,
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
        "posted_ever": 300,
        "last_post_age_seconds": 4 * HOUR,
        "intents_ever": 500,
        "oldest_intent_age_seconds": 90 * 24 * HOUR,
        "oldest_active_destination_age_seconds": 120 * 24 * HOUR,
        "debited_total": 300,
        "ledger_days": 140,
        "accounts_active": 2,
    }
    base.update(over)
    return json.dumps(base)


def read(body, *, watched_s=10 * 24 * HOUR, silence_s=SILENCE, grace_s=GRACE):
    return classify(
        200, body, silence_s=silence_s, grace_s=grace_s, watched_s=watched_s
    )


class TestNoPostYetIsOnADeadline:
    """`never-posted` must EXPIRE into an alert. The scheduling classifier's
    `no-signal` does not, and a permanent one here would excuse an estate
    that never posts."""

    #: No landings and no destination rung, so each test below opts exactly one
    #: anchor back in and nothing else can trip the grace for it.
    #:
    #: `accounts_active=0` travels WITH the null destination age deliberately:
    #: they are one fact told twice, and the pair guard in `classify` refuses a
    #: payload where they disagree: the base `accounts_active=2` beside a null
    #: age is an estate that cannot exist.
    NEVER = dict(
        posted_ever=0,
        last_post_age_seconds=None,
        accounts_active=0,
        oldest_active_destination_age_seconds=None,
        debited_total=0,
        ledger_days=0,
    )

    def test_past_the_grace_the_SAME_reading_becomes_an_alert(self):
        """One payload, two ages of the watch clock. Nothing about the estate
        changed — which is the point: the clock is what makes it speak."""
        body = payload(**self.NEVER, intents_ever=0, oldest_intent_age_seconds=None)
        assert read(body, watched_s=GRACE - HOUR).state == NEVER_POSTED
        assert read(body, watched_s=GRACE + HOUR).state == NEVER_POSTED_OVERDUE

    def test_the_expiry_boundary_is_inclusive_and_does_not_straddle(self):
        body = payload(**self.NEVER, intents_ever=0, oldest_intent_age_seconds=None)
        assert read(body, watched_s=GRACE - 1).state == NEVER_POSTED
        assert read(body, watched_s=GRACE).state == NEVER_POSTED_OVERDUE

    def test_the_oldest_intent_trips_the_grace_on_a_FRESH_state_file(self):
        """The second anchor. A watcher that starts today on an estate that has
        held intents for a week must not restart the clock — its own record
        can be LOST, and a lost clock is the one direction a classifier must
        not fail in quietly."""
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
        intent. Inside that window `oldest_intent_age_seconds` is null, so a
        watcher that started there would have no database-side anchor at all
        and would sit on a NOTICE past the grace while destinations idle.
        """
        v = read(
            payload(**self.NEVER, intents_ever=0, oldest_intent_age_seconds=None),
            watched_s=0.0,
        )
        assert v.state == NEVER_POSTED, "no rung yet — this must still be a notice"

        v = read(
            payload(
                **{
                    **self.NEVER,
                    "accounts_active": 2,
                    "oldest_active_destination_age_seconds": GRACE + HOUR,
                },
                intents_ever=0,
                oldest_intent_age_seconds=None,
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
                    "accounts_active": 2,
                    "oldest_active_destination_age_seconds": GRACE + HOUR,
                },
                oldest_intent_age_seconds=GRACE + 10 * HOUR,
            ),
            watched_s=GRACE + 5 * HOUR,
        )
        assert "the oldest intent" in v.detail

        v = read(
            payload(**self.NEVER, intents_ever=0, oldest_intent_age_seconds=None),
            watched_s=0.0,
        )
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
    """An empty estate is not exempt.

    An empty estate: 0 destinations, 0 intents, nothing that could mint. The
    tempting design gates the alert on `accounts_active > 0` — "nothing is
    expected to post, so stay quiet" — which is true, useless, and excuses
    this state forever.
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
    """`accounts_active` is alert TEXT and nothing else.

    Isolated between two NONZERO counts at the same destination age, because
    the pair guard binds the count to the age: zero destinations forces a null
    age, which would change the ladder as well as the count and stop isolating
    anything. The zero case is `test_an_empty_estate_still_alerts`.
    """
    never = dict(
        posted_ever=0,
        last_post_age_seconds=None,
        intents_ever=6,
        oldest_intent_age_seconds=2 * 24 * HOUR,
        oldest_active_destination_age_seconds=3 * 24 * HOUR,
        debited_total=0,
        ledger_days=0,
    )
    one = read(payload(**never, accounts_active=1), watched_s=GRACE + HOUR)
    many = read(payload(**never, accounts_active=5), watched_s=GRACE + HOUR)
    assert one.state == many.state == NEVER_POSTED_OVERDUE


class TestBothStuckPhasesAreFalse:
    """Both stuck phases (#1268), stated as a test.

    Two stacked causes with opposite remedies, and `/health/scheduling` reads
    `healthy` across both, every field of it true. The one assertion false in
    both is *a post landed*.
    """

    #: Empty tier: nothing could mint.
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

    #: Populated and minting; six intents stranded `awaiting_approval`, which is
    #: a NOMINAL state for every field the scheduling classifier reads.
    #: The destinations predate the intents; that gap is exactly the window the
    #: destination rung covers.
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

    def test_the_two_phases_are_told_apart_in_the_alert_text(self):
        """Same verdict, opposite remedies — connect a destination, or unblock
        approvals. An alert that could not distinguish them would be correct and
        useless."""
        a = read(payload(**self.PHASE_A), watched_s=16 * 24 * HOUR).detail
        b = read(payload(**self.PHASE_B), watched_s=16 * 24 * HOUR).detail
        assert "nothing could mint a post" in a
        assert "stuck before publishing" in b
        assert a != b


def test_debits_without_landings_are_called_out_as_such():
    """The cap ledger (#1268), earning its place in the CONTRAST.

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


class TestTheHealthyAndSilentSplit:
    def test_the_threshold_boundary(self):
        assert read(payload(last_post_age_seconds=SILENCE)).state == POSTING
        assert read(payload(last_post_age_seconds=SILENCE + 1)).state == SILENT


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
            ("oldest_intent_age_seconds", {"intents_ever": 0}),
            ("oldest_active_destination_age_seconds", {"accounts_active": 0}),
        ],
    )
    def test_a_missing_age_is_unreachable_even_though_NULL_is_legal(
        self, key, null_is_legal_when
    ):
        """The sentinel's whole job. `null` means "there has never been one";
        an absent key means the endpoint is not serving the field, and reading
        the second as the first would invent a fact.

        Each case zeroes the age's PAIRED COUNT, because a null age beside a
        nonzero count is a contradiction the pair guard refuses — a different
        rejection from the one under test here.
        """
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


class TestEveryCountAndAgePairMustAgree:
    """One fact from two directions, so they can contradict — and a
    contradiction is an instrument fault neither field reveals alone.

    **All three pairs.** The two pairs feeding the grace ladder are guarded as
    the landing pair is: a null age there would fall into `float(None or 0)`
    and contribute zero elapsed (#1269).
    """

    #: (count field, age field, a count that must force a non-null age)
    PAIRS = [
        ("posted_ever", "last_post_age_seconds", 7),
        ("intents_ever", "oldest_intent_age_seconds", 6),
        ("accounts_active", "oldest_active_destination_age_seconds", 2),
    ]

    @pytest.mark.parametrize("count_key, age_key, n", PAIRS)
    def test_a_count_with_no_age_is_unreachable(self, count_key, age_key, n):
        v = read(payload(**{count_key: n, age_key: None}))
        assert v.state == UNREACHABLE
        assert count_key in v.detail and age_key in v.detail

    @pytest.mark.parametrize("count_key, age_key, n", PAIRS)
    def test_an_age_with_a_zero_count_is_unreachable(self, count_key, age_key, n):
        assert read(payload(**{count_key: 0, age_key: 100})).state == UNREACHABLE

    @pytest.mark.parametrize("count_key, age_key, n", PAIRS)
    def test_it_is_not_resolved_toward_the_reassuring_reading(
        self, count_key, age_key, n
    ):
        """A contradiction resolved toward the healthy branch would be the
        fail-toward-good-news class this whole instrument exists for."""
        for v in (
            read(payload(**{count_key: n, age_key: None})),
            read(payload(**{count_key: 0, age_key: 100})),
        ):
            assert v.state != POSTING


class TestAMissingLadderAgeCannotSILENCEAnOverdueEstate:
    """The failure the pair guard exists to stop, stated as its consequence.

    A null age on a ladder pair must not read as **zero elapsed**: the rung
    would contribute nothing, and the verdict would drop from
    `never-posted-overdue` to `never-posted` while the estate is genuinely
    overdue — quietly, in the direction that looks fine.

    Not reachable from the SQL — each pair comes from one `count(*)` and one
    `max(...)` over one population in one statement. This is the guard for the
    edit that decouples them, and `posting_health` already filters one side of
    one pair deliberately (the landing filter in `fn_health_posting_freshness`, 081), so that edit has a precedent in the
    very file that produces these fields.
    """

    OVERDUE = dict(
        posted_ever=0,
        last_post_age_seconds=None,
        intents_ever=6,
        oldest_intent_age_seconds=400000,
        accounts_active=2,
        oldest_active_destination_age_seconds=400000,
        debited_total=0,
        ledger_days=0,
    )

    LADDER_AGES = ["oldest_intent_age_seconds", "oldest_active_destination_age_seconds"]

    @pytest.mark.parametrize("age_key", LADDER_AGES)
    def test_nulling_a_ladder_age_refuses_instead_of_going_quiet(self, age_key):
        v = read(payload(**{**self.OVERDUE, age_key: None}), watched_s=0.0)
        assert v.state == UNREACHABLE, (
            "a null ladder age must not read as zero elapsed — that turns a "
            "`never-posted-overdue` verdict into a plain notice on an overdue estate"
        )
        assert v.state != NEVER_POSTED


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
    to a silence alert would produce "N post(s) landed in total, and ... nothing
    is landing" — an alert arguing with itself.
    """
    v = read(payload(posted_ever=300, last_post_age_seconds=SILENCE + 1))
    assert v.state == SILENT
    assert "nothing is landing" not in v.detail
    assert "stuck before publishing" not in v.detail
    # What it says instead: the arithmetic. Debits far above landings means
    # publishing is being reached and failing; level means nothing reaches it.
    assert "300 cap debit(s) against 300 landing(s)" in v.detail


def test_the_silence_alert_surfaces_attempts_that_outnumber_landings():
    v = read(
        payload(posted_ever=300, last_post_age_seconds=SILENCE + 1, debited_total=500)
    )
    assert v.state == SILENT
    assert "500 cap debit(s) against 300 landing(s)" in v.detail
