"""Classification of one `/health/posting` reading into a verdict.

Pure functions over the endpoint's JSON: posts are landing, have gone silent,
or have never landed within the grace period. `classify` is the entry point.
"""

from __future__ import annotations

import json


#: Two full product cycles. `daily_post_counts` is keyed on `local_date` and
#: `posts_per_day` is a per-DAY quantity (`ck_ws_posts_per_day BETWEEN 1 AND
#: 50`, default 3), so one cycle is 24h and this tolerates one entirely blank
#: day — an empty media pool, every slot skipped — before speaking.
#:
#: The margin is deliberate: a threshold near one day buys nothing and costs
#: false alarms on a quiet day. Scheduling stalls are the scheduling
#: classifier's to report, within minutes.
DEFAULT_SILENCE_THRESHOLD_S = 48 * 3600

#: How long an estate may have never posted before `never-posted` becomes
#: `never-posted-overdue`. A SEPARATE knob from the silence threshold and not a duplicate of
#: it: silence is measured against a cadence the estate has DEMONSTRATED, and
#: this is measured against no cadence at all — only the expectation that a
#: deployed product posts. It covers onboarding end to end (a workspace, an
#: account, a media source, and reaching the first slot), which is why it is the
#: longer of the two.
DEFAULT_GRACE_S = 72 * 3600


POSTING = "posting"
SILENT = "silent"
NEVER_POSTED = "never-posted"
NEVER_POSTED_OVERDUE = "never-posted-overdue"
UNREACHABLE = "unreachable"


#: Distinguishes "the key is absent" from "the key is null". `None` cannot do
#: it, because `None` is a legal VALUE for two of these fields.
_ABSENT = object()

#: Counts. Every one of them must be present and whole.
_COUNTS = (
    "posted_ever",
    "intents_ever",
    "debited_total",
    "ledger_days",
    "accounts_active",
)

#: Ages. `null` is legal and MEANINGFUL — it is how the endpoint says "there has
#: never been one" without returning the reassuring `0`. A MISSING key is not
#: legal, which is why these are read through the sentinel.
_AGES = (
    "last_post_age_seconds",
    "oldest_intent_age_seconds",
    "oldest_active_destination_age_seconds",
)

#: (count, age) over ONE population, so `count > 0` and `age is not None` are
#: the same fact told twice and MUST agree. Every pair is checked, not just the
#: first — see `classify`. Adding an axis means adding its pair here.
_PAIRS = (
    ("posted_ever", "last_post_age_seconds"),
    ("intents_ever", "oldest_intent_age_seconds"),
    ("accounts_active", "oldest_active_destination_age_seconds"),
)


class Verdict:
    """What one reading means, before any history is applied."""

    def __init__(self, state: str, detail: str, payload: dict | None = None):
        self.state = state
        self.detail = detail
        self.payload = payload or {}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Verdict({self.state!r}, {self.detail!r})"

    def __eq__(self, other) -> bool:
        return isinstance(other, Verdict) and (self.state, self.detail) == (
            other.state,
            other.detail,
        )


def _is_count(v) -> bool:
    """A JSON number that must be a whole count.

    `bool` is an `int` in Python and `true` is not a count, so it is excluded
    explicitly — the scheduling classifier applies the same rule.
    """
    return isinstance(v, int) and not isinstance(v, bool)


def _is_count_or_null(v) -> bool:
    return v is None or _is_count(v)


def _hours(seconds: float) -> str:
    """Ages here run to days: hours below 48, days above, so a reader never has
    to convert a large count of seconds."""
    h = seconds / 3600.0
    return f"{h:.0f}h" if h < 48 else f"{h / 24:.1f}d"


def _describe_estate(data: dict) -> str:
    """The half of the verdict that says WHICH failure this is.

    Context only. Different failures read `posted_ever = 0` with opposite
    remedies — connect a destination, or unblock approvals — so a verdict that
    could not tell them apart would be correct and useless.
    """
    active, intents = data["accounts_active"], data["intents_ever"]
    debited, buckets = data["debited_total"], data["ledger_days"]
    posted = data["posted_ever"]
    if posted > 0:
        # THE `silent` PATH. Every branch below diagnoses an estate that has
        # NEVER posted, and appending one to a silence verdict makes it
        # contradict itself — "N post(s) landed in total, and... nothing is
        # landing". The useful context here is the arithmetic instead: debits
        # far above landings means publishing is being reached and failing,
        # debits level with landings means nothing is reaching it at all.
        return (
            f"{active} active destination(s), {intents} intent(s), and "
            f"{debited} cap debit(s) against {posted} landing(s)"
        )
    if active == 0 and intents == 0:
        return (
            "no active destinations and no intents have ever been created, so "
            "nothing could mint a post"
        )
    if intents == 0:
        return (
            f"{active} active destination(s) but no intent has ever been "
            f"created, so nothing reached the schedule"
        )
    # `debited_total` alone CANNOT separate the next two, and they have
    # different remedies. `publish_cap` refunds by DECREMENTING the same
    # counter (an UPDATE, never a delete), so an estate that claimed a slot
    # every time and failed every time after the debit sums back to zero —
    # reading identically to one that never reached publishing at all. The
    # day-bucket row survives that at `count = 0` (`ck_dpc_nonneg`), so it is
    # what tells "never tried" from "tried and gave it all back".
    #
    # It would NOT survive `04_functions.sql`'s retention purge, which deletes the row
    # outright — but `retention_sweep` sweeps only `rate_counters`, never this
    # table. If this class is ever swept, this distinction goes with it.
    if debited == 0 and buckets == 0:
        return (
            f"{intents} intent(s) created against {active} active "
            f"destination(s), and none has ever claimed a publish slot — they "
            f"are stuck before publishing (approval is the usual cause)"
        )
    if debited == 0:
        return (
            f"{intents} intent(s), {active} active destination(s), and "
            f"{buckets} day-bucket(s) that claimed a publish slot with every "
            f"debit REFUNDED — publishing is being reached and failing after "
            f"the cap debit"
        )
    return (
        f"{intents} intent(s), {active} active destination(s), and "
        f"{debited} cap debit(s) — publishing is being ATTEMPTED and nothing "
        f"is landing"
    )


def classify(
    status: int,
    body: str,
    *,
    silence_s: int,
    grace_s: int,
    watched_s: float,
) -> Verdict:
    """One reading → one state. Pure; the whole reason this file is testable.

    `watched_s` is how long the caller has been watching the estate. It is an
    input rather than
    something derived later, so that a state is fully determined by one call and
    every transition is a unit test — including the one that only exists because
    of history, `never-posted` becoming `never-posted-overdue`.

    **Strict about the payload, deliberately.** A missing or mistyped key is
    `unreachable`, never healthy. The lenient spelling — `data.get("posted_ever",
    1)` — turns a broken instrument into a clean bill of health, which is the
    exact failure this classifier exists to prevent, one layer inward. Every key
    is strict here: unlike the scheduling classifier's cursor axis there is no
    shipped contract to keep compatible, so the asymmetry it documents does not
    apply.
    """
    if status != 200:
        # A caller reports a transport failure as status 0 and puts the exception
        # in the body. Surface that rather than "HTTP 0", which says nothing
        # about whether it is DNS, TLS or a refused port.
        return Verdict(
            UNREACHABLE, body.strip()[:200] if status == 0 else f"HTTP {status}"
        )
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return Verdict(UNREACHABLE, "response was not JSON")
    if not isinstance(data, dict):
        return Verdict(UNREACHABLE, "response was not an object")

    for key in _COUNTS:
        if not _is_count(data.get(key)):
            return Verdict(UNREACHABLE, f"{key} missing or not an integer")
    for key in _AGES:
        if not _is_count_or_null(data.get(key, _ABSENT)):
            return Verdict(UNREACHABLE, f"{key} was neither null nor an integer")

    # EVERY pair, not just the landing one. Each names ONE fact from two
    # directions, so a disagreement is an instrument fault neither field
    # reveals alone — reported as `unreachable` rather than resolved toward
    # either, because a count and an age that contradict each other cannot be
    # trusted in the direction they happen to favour.
    #
    # Without this check on the ladder pairs, a null age with a nonzero count
    # would fall into `float(None or 0)` and contribute **zero elapsed** to the
    # grace clock: the same reassuring `0` this module rejects two docstrings
    # above. The verdict would drop from `never-posted-overdue` to
    # `never-posted` while the estate is overdue — quiet, in the direction that
    # looks fine.
    #
    # Not reachable from the SQL — each pair is one `count(*)` and one
    # `max(...)` over one population in one statement. It becomes reachable the
    # moment a filter lands on one side of a pair, which `posting_health`
    # already does deliberately once (the landing filter in `fn_health_posting_freshness`, `fleet health functions`), so the precedent for the
    # decoupling lives in the very file that produces these fields.
    for count_key, age_key in _PAIRS:
        count, paired_age = data[count_key], data[age_key]
        if (count > 0) != (paired_age is not None):
            return Verdict(
                UNREACHABLE,
                f"{count_key}={count} contradicts {age_key}="
                f"{'null' if paired_age is None else paired_age}",
            )

    posted, age = data["posted_ever"], data["last_post_age_seconds"]

    if posted > 0:
        if age > silence_s:
            return Verdict(
                SILENT,
                f"nothing has posted for {_hours(age)} (threshold "
                f"{_hours(silence_s)}); {_describe_estate(data)}",
                data,
            )
        return Verdict(
            POSTING,
            f"last post {_hours(age)} ago, {posted} landed in total",
            data,
        )

    # NOTHING HAS EVER LANDED. The grace clock decides whether that is a fresh
    # deployment or a stuck estate, and it takes the LATER of the anchors so
    # none can suppress another.
    # THE LADDER. Every rung dates the estate's expectation to post from a
    # different point, and the clock takes the LATEST of them, so no single one
    # can suppress the others. Each DB-side rung can only ever make this speak
    # SOONER — an estate that has held a destination for a week and never
    # posted is not waiting, it is stuck — which is why adding one is always
    # safe and why `first_seen_at` is a floor rather than the answer.
    #
    # `max` over (seconds, name) pairs keyed on the seconds ONLY, so ties go to
    # the FIRST entry and a fresh estate reads as the caller's own clock rather
    # than a zero-age rung.
    #
    # The `key=` pins the INTENT: rank by time, never by spelling. Whole-tuple
    # comparison only reaches element 1 on an exact tie, and these names happen
    # to sort so that the first is also largest alphabetically, so both
    # forms agree; the `key=` keeps them agreeing when a rung is renamed or
    # reordered.
    elapsed, anchor = max(
        (
            (watched_s, "this monitor started watching"),
            (
                float(data["oldest_active_destination_age_seconds"] or 0),
                "the oldest active destination was connected",
            ),
            (float(data["oldest_intent_age_seconds"] or 0), "the oldest intent"),
        ),
        key=lambda rung: rung[0],
    )
    if elapsed >= grace_s:
        return Verdict(
            NEVER_POSTED_OVERDUE,
            f"no post has EVER landed, and {_hours(elapsed)} have passed since "
            f"{anchor} (grace {_hours(grace_s)}); {_describe_estate(data)}",
            data,
        )
    return Verdict(
        NEVER_POSTED,
        f"no post has landed yet, {_hours(elapsed)} since {anchor} (grace "
        f"{_hours(grace_s)}); {_describe_estate(data)}",
        data,
    )
