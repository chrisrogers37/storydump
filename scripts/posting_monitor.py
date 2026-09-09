"""The check that asserts A POST LANDED, and alarms on its absence (#1268).

## Why the existing monitor could not be extended into this

`scheduling_monitor.py` beside this file polls `/health/scheduling` and is
correct. It reads the CLOCK and the WORKER, and both were entirely healthy
through a sixteen-day silence in which nothing posted — 1936 consecutive
`healthy` readings, ~6.7 days of them, **every published field true**.

`scheduling_health`'s own docstring names the gap as a bound:

> **It cannot see a failure PAST the mint.** Cursors advancing while nothing
> posts is a different outage and wants its own signal.

The outage had two stacked causes and one green light across both:

- **08-24 → ~09-02** — the tier was EMPTY: 0 `ig_accounts`, 0 `workspaces`, 0
  `media_sources`. Nothing could mint an intent.
- **09-07 → onward** — populated, minting, and all six intents stranded
  `awaiting_approval`. `approval_mode` defaults to `manual`, so an intent
  awaiting approval is **not overdue — it is waiting correctly**.

Different causes, opposite remedies, identical reading. The single assertion
false in both is *a post landed*.

## The four constraints this is built to, each from a way an instrument failed

1. **Not a runbook step.** A halted runbook stops running its own checks, which
   is exactly the case that needs them. This is a timer, owned by no plan and no
   phase.
2. **Not derived from clock or worker state.** Those stayed true through both
   phases. Nothing here reads `jobs`, `next_slot_at`, a heartbeat or a cursor.
3. **Alerts on absence, WITH A CLOCK** — see the next section, which is the only
   part of this file that is not the sibling's shape reused.
4. **Off-host alert path.** Runs on the fleet host; the alert shares no machine,
   process, network path, clock or channel with its subject.

## `no-signal` MUST EXPIRE HERE, and that is the whole design

The sibling draws the distinction this instrument depends on — *nothing is late*
versus *nothing EXISTS to be late* — and resolves it with a `no-signal` state
that **never alerts**, only re-states weekly. On the cursor axis that is right:
an estate with no destinations is a legitimate pre-launch state, and the remedy
is to connect one, not to page someone.

**On the posting axis the same shape is the outage.** Phase (a) above is
precisely "nothing exists to be late": an empty tier, no destinations, nothing
that could mint. A permanent `no-signal` would have excused all sixteen days,
inside the instrument built to end them.

So this splits it in two and puts a clock between them:

- **`never-posted`** — nothing has ever landed and the grace has not run out.
  A NOTICE. Expected on a fresh deployment.
- **`never-posted-overdue`** — nothing has ever landed and the grace HAS run
  out. **An ALERT.**

The second is what the first becomes. That is #1268's third requirement stated
mechanically: *"no posts yet" must expire into "no posts in N hours"*.

### The grace clock is anchored where it cannot be excused away

The obvious anchor is `accounts_active > 0` — stay quiet until a destination
exists, because until then nothing is expected to post. **Rejected, and this is
the load-bearing rejection**: phase (a) *is* zero destinations, so that anchor
excuses the very state it would need to catch, forever. Same class as the gauge
this replaces: an explanation for the silence that is true and useless.

The anchor is instead the LATER of two clocks, so neither can suppress it:

- **how long this monitor has been watching**, from its own state file — off
  host, and nothing the app does can reset it; and
- **how long the oldest intent has existed** (`oldest_intent_age_seconds`),
  which only ever makes this speak SOONER — an estate that has had something to
  post for four days and has never posted is not waiting, it is stuck.

In phase (a) there were no intents and the first clock is the only one. In phase
(b) intents existed, and the second fires even on a monitor deployed yesterday.

**`accounts_active` is still read** — for the alert TEXT, so a human is told
which of the two failures they are looking at. It never reaches the verdict, and
`test_an_empty_estate_still_alerts` is what keeps it that way.

## The evidence is `post_intents`, not the cap ledger

#1268 names `SELECT local_date, sum(count) FROM daily_post_counts`. That table
is read and reported here, but **the verdict does not rest on it**, and the
reason is a fact about when it is written rather than a preference.

`publish_cap` debits it at the `approved → publishing` flip — cap debit and
state change coupled in one CTE — which happens **before the publish call**. So
`count > 0` means an attempt claimed a cap slot, not that anything reached
Instagram. A publish path that debits and fails every time moves that number and
lands nothing, and a monitor resting on it would go green on exactly the failure
it exists to catch. That is this issue's own shape, one layer inward.

`post_intents.state = 'posted'` is the landing, and `ck_posted_complete` makes
the database refuse such a row unless it carries provider evidence
(`ig_container_id`, `publish_step = 'effect_confirmed'`) or is a confirmed
manual post. The strongest assertion available is one the schema will not let a
row violate. (`published_via = 'legacy_backfill'` is the one exemption that
constraint grants, so `posting_health` filters it out — see there.)

The ledger still earns its place, in the CONTRAST: `debited_total > 0` beside
`posted_ever = 0` is *attempts are being made and none is landing*, a sharper
diagnosis than either number alone, and it goes in the alert text.

## Bound, stated because it will not be obvious later

**No verdict here has ever been raised by real posting traffic.** The estate has
never posted through the target tier — `daily_post_counts` holds zero rows,
ever — so `posting` and `silent` are exercised against captured payloads and
mutation checks, never against a real landing. `never-posted-overdue` is the one
state production can currently produce, and it is the one that matters today.

A later reader must not read *tested* for *seen in production*. The first real
post is what turns this from a monitor that is correct into a monitor that is
proven — and this file's own subject is that the difference is easy to miss.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

#: Two full product cycles. `daily_post_counts` is keyed on `local_date` and
#: `posts_per_day` is a per-DAY quantity (`ck_ws_posts_per_day BETWEEN 1 AND
#: 50`, default 3), so one cycle is 24h and this tolerates one entirely blank
#: day — an empty media pool, every slot skipped — before speaking.
#:
#: The margin is the point, as it is for the sibling's 600s. The gap between a
#: daily cadence and a sixteen-day silence is nearly three orders of magnitude;
#: a threshold near one day buys nothing and costs false alarms on a quiet
#: Sunday. It is deliberately NOT tight enough to catch the 18- and 19-hour
#: scheduling outages — those are the sibling's, and it sees them in minutes.
DEFAULT_SILENCE_THRESHOLD_S = 48 * 3600

#: How long an estate may have never posted before that is an alert rather than
#: a notice. A SEPARATE knob from the silence threshold and not a duplicate of
#: it: silence is measured against a cadence the estate has DEMONSTRATED, and
#: this is measured against no cadence at all — only the expectation that a
#: deployed product posts. It covers onboarding end to end (a workspace, an
#: account, a media source, and reaching the first slot), which is why it is the
#: longer of the two.
DEFAULT_GRACE_S = 72 * 3600

#: While a fault persists, repeat rather than going quiet — a long outage must
#: not look like a resolved one. The sibling's cadence, for the sibling's reason.
REALERT_AFTER_S = 6 * 3600

#: The pre-grace notice. Rare, but never silent: "this product has not posted
#: yet and we are still inside the window" must not fade from memory just
#: because it was said once.
RENOTICE_AFTER_S = 7 * 24 * 3600

POSTING = "posting"
SILENT = "silent"
NEVER_POSTED = "never-posted"
NEVER_POSTED_OVERDUE = "never-posted-overdue"
UNREACHABLE = "unreachable"

#: Nothing to say. Distinct from the alerting codes so a supervisor can route on
#: them without parsing text. The sibling's codes, deliberately identical: an
#: operator reading two units should not have to learn two conventions.
EXIT_QUIET, EXIT_SPOKE, EXIT_NOTIFY_FAILED = 0, 10, 11

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
    explicitly — the sibling established this and the same payloads reach here.
    """
    return isinstance(v, int) and not isinstance(v, bool)


def _is_count_or_null(v) -> bool:
    return v is None or _is_count(v)


def _hours(seconds: float) -> str:
    """Ages in this instrument run to days. A human woken at 3am should not have
    to divide 1382400 by 3600 to find out whether this is bad."""
    h = seconds / 3600.0
    return f"{h:.0f}h" if h < 48 else f"{h / 24:.1f}d"


def _describe_estate(data: dict) -> str:
    """The half of the alert that says WHICH failure this is.

    Context only. Both halves of the 2026 outage read `posted_ever = 0`, and the
    remedies are opposite — connect a destination, or unblock approvals — so an
    alert that could not tell them apart would be correct and useless.
    """
    active, intents = data["accounts_active"], data["intents_ever"]
    debited, buckets = data["debited_total"], data["ledger_days"]
    posted = data["posted_ever"]
    if posted > 0:
        # THE `silent` PATH. Every branch below diagnoses an estate that has
        # NEVER posted, and appending one to a silence alert makes the alert
        # contradict itself — "412 post(s) landed in total, and ... nothing is
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
    # It would NOT survive `059`'s retention purge, which deletes the row
    # outright — but `retention_sweep` is parked in `work_loop.UNBUILT_KINDS`
    # and has never run. If it is ever built, this distinction goes with it.
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

    `watched_s` is how long this monitor has been watching, which the caller
    reads from the state file BEFORE polling. It is an input rather than
    something derived later, so that a state is fully determined by one call and
    every transition is a unit test — including the one that only exists because
    of history, `never-posted` becoming `never-posted-overdue`.

    **Strict about the payload, deliberately.** A missing or mistyped key is
    `unreachable`, never healthy. The lenient spelling — `data.get("posted_ever",
    1)` — turns a broken instrument into a clean bill of health, which is the
    exact failure this poller exists to prevent, one layer inward. Every key is
    strict here: unlike the sibling's cursor axis there is no shipped contract to
    keep compatible, so the asymmetry it documents does not apply.
    """
    if status != 200:
        # `fetch` reports a transport failure as status 0 and puts the exception
        # in the body. Surface that rather than "HTTP 0", which tells a human
        # woken at 3am nothing about whether it is DNS, TLS or a refused port.
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

    posted, age = data["posted_ever"], data["last_post_age_seconds"]

    # The two fields describe ONE fact from two directions, so they can
    # disagree — and a disagreement is an instrument fault that neither field
    # reveals alone. Reported as `unreachable` rather than resolved toward
    # either: a count and an age that contradict each other mean the reading
    # cannot be trusted in the direction it happens to favour.
    if (posted > 0) != (age is not None):
        return Verdict(
            UNREACHABLE,
            f"posted_ever={posted} contradicts last_post_age_seconds="
            f"{'null' if age is None else age}",
        )

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
    # deployment or the outage, and it takes the LATER of the two anchors so
    # neither can suppress the other — see the module docstring.
    # THE LADDER. Every rung dates the estate's expectation to post from a
    # different point, and the clock takes the LATEST of them, so no single one
    # can suppress the others. Each DB-side rung can only ever make this speak
    # SOONER — an estate that has held a destination for a week and never
    # posted is not waiting, it is stuck — which is why adding one is always
    # safe and why `first_seen_at` is a floor rather than the answer.
    #
    # `max` over (seconds, name) pairs keyed on the seconds ONLY, so ties go to
    # the FIRST entry and a fresh estate reads as the monitor's own clock rather
    # than a zero-age rung.
    #
    # MEASURED, because the obvious claim is wrong: dropping `key=` here is an
    # INERT mutant today, not a live bug. Whole-tuple comparison only reaches
    # element 1 on an exact tie, and these three names happen to sort so that
    # "this monitor started watching" is both first in the list and largest
    # alphabetically — the same answer either way. The `key=` pins the INTENT
    # (rank by time, never by spelling) and would become load-bearing the moment
    # a rung is renamed or reordered. Do not go looking for the test that kills
    # it; there is none, and that is the finding rather than a gap.
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


def announce(state: dict, verdict: Verdict, now: float) -> dict:
    """Record that a human was actually told. Called ONLY after delivery
    succeeds.

    `announced` is deliberately separate from `state`: the first is what a human
    knows, the second is what the endpoint last said. Collapsing them is a live
    bug rather than a tidiness question — with one field a failed notify still
    advances the history, so the next poll sees "same as last time" and stays
    quiet about a message nobody received.
    """
    state = dict(state)
    state["announced"] = verdict.state
    state["spoke_at"] = now
    return state


def decide(verdict: Verdict, prior: dict, now: float) -> tuple[dict, str | None]:
    """Verdict + history → (state to persist, message to send or None).

    Pure, so every transition is a unit test rather than a stakeout. It does NOT
    record that it spoke — only `announce` does, and only the caller knows
    whether delivery worked.

    `first_seen_at` is carried forward untouched whenever it already exists.
    That field IS the grace clock, so re-seeding it on any path would hand the
    app a way to reset a deadline it must not be able to reach.
    """
    announced = prior.get("announced")
    # A run of the SAME state the human already knows about; anything else means
    # they have not been told, whatever the endpoint has been saying.
    spoke_at = prior.get("spoke_at", 0.0) if announced == verdict.state else 0.0
    run = prior.get("consecutive", 0) + 1 if prior.get("state") == verdict.state else 1
    state = {
        "state": verdict.state,
        "detail": verdict.detail,
        "consecutive": run,
        "observed_at": now,
        "first_seen_at": prior.get("first_seen_at", now),
        "announced": announced,
        "spoke_at": prior.get("spoke_at", 0.0),
        "payload": verdict.payload,
    }

    def repeat_due(interval: float) -> bool:
        """Never told, or told long enough ago that silence would read as
        fixed."""
        return not spoke_at or now - spoke_at >= interval

    if verdict.state == UNREACHABLE:
        # One failed request is a dropped packet. Two is a fact.
        if run < 2 or not repeat_due(REALERT_AFTER_S):
            return state, None
        return state, (
            f"FLEET ALERT: storydump posting check UNREACHABLE "
            f"({verdict.detail}) on {run} consecutive polls. The detector "
            f"cannot look, which is not the same as posting being fine."
        )

    if verdict.state == SILENT:
        # Pages on the FIRST reading, unlike UNREACHABLE. Not two tunings of one
        # knob: an age past the threshold already CONTAINS its duration — two
        # days of silence is what the number means — so the observation is the
        # sustain, and confirming it would only add delay to something already
        # proven. A failed request contains no duration at all.
        if not repeat_due(REALERT_AFTER_S):
            return state, None
        return state, (
            f"FLEET ALERT: storydump HAS STOPPED POSTING — {verdict.detail}. "
            f"The scheduling monitor cannot see this: it reads the clock and "
            f"the worker, and both stay true while nothing posts."
        )

    if verdict.state == NEVER_POSTED_OVERDUE:
        # First reading, for the reason above: the elapsed grace IS the
        # duration. This is the state production is in today.
        if not repeat_due(REALERT_AFTER_S):
            return state, None
        return state, (
            f"FLEET ALERT: storydump HAS NEVER POSTED — {verdict.detail}. This "
            f"is no longer a new deployment waiting for its first post; the "
            f"grace window has passed. A prior silence in this class ran 16 "
            f"days with the scheduling monitor reporting healthy throughout."
        )

    if verdict.state == NEVER_POSTED:
        # NOT an alert, and NOT an all-clear. Expected on a fresh deployment —
        # and it is on a deadline, which is the difference between this state
        # and the sibling's permanent `no-signal`.
        if not repeat_due(RENOTICE_AFTER_S):
            return state, None
        return state, (
            f"storydump posting: NO POST YET — {verdict.detail}. This is not "
            f"an alert and not an all-clear: nothing has landed, and the only "
            f"reason that is not being raised is that the grace window has not "
            f"passed. It will be raised when it does."
        )

    # POSTING. Silence is right, except on the two transitions that would
    # otherwise be invisible — and those are keyed on what the human was last
    # TOLD, not on what the endpoint last said.
    if announced in (SILENT, UNREACHABLE, NEVER_POSTED_OVERDUE):
        return state, (f"RECOVERED: storydump is posting again — {verdict.detail}.")
    if announced == NEVER_POSTED:
        return state, (
            f"storydump posting: FIRST POST OBSERVED — {verdict.detail}. This "
            f"check now has a real cadence to measure against."
        )
    return state, None


def fetch(url: str, timeout_s: float) -> tuple[int, str]:
    """The endpoint, or a status that says why not. Never raises."""
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:  # noqa: S310
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - any failure to reach it is one state
        return 0, f"{type(exc).__name__}: {exc}"


def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        # A first run and a corrupt file both mean "no history".
        #
        # For the sibling that only ever made it speak SOONER. Here it also
        # restarts the grace clock, which makes this poller speak LATER — the
        # one direction a monitor must not fail in silently. It is bounded
        # rather than ignored: `oldest_intent_age_seconds` is the second anchor
        # and the app supplies it, so an estate with standing intents still
        # trips the grace on the very next poll, and the message names which
        # anchor it used.
        return {}


def save_state(path: str, state: dict) -> None:
    """Atomic, so a kill mid-write cannot leave a file that reads as a fresh run
    forever — which here would also reset the grace clock on every poll and
    silence the overdue state permanently."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".posting-monitor.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def notify(command: str, message: str) -> bool:
    """Hand the message to whatever this deployment uses to reach a human.

    The command is CONFIG, not code: the fleet path lives in the systemd unit,
    so this repository holds no fleet-specific path and the coupling that keeps
    script and endpoint together stays inside one repo and one test suite.
    """
    try:
        done = subprocess.run(  # noqa: S603
            [command, message], capture_output=True, text=True, timeout=60
        )
        return done.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", required=True, help="the /health/posting endpoint")
    ap.add_argument("--state-file", required=True)
    ap.add_argument(
        "--notify-command", help="executable given one argument: the message"
    )
    ap.add_argument(
        "--silence-threshold",
        type=int,
        default=DEFAULT_SILENCE_THRESHOLD_S,
        help="seconds since the last landed post before posting is called "
        "stopped; must exceed the daily posting cycle",
    )
    ap.add_argument(
        "--grace",
        type=int,
        default=DEFAULT_GRACE_S,
        help="seconds an estate may have never posted before that becomes an "
        "alert rather than a notice",
    )
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument(
        "--status",
        action="store_true",
        help="print the last recorded state and exit without polling",
    )
    args = ap.parse_args(argv)

    if args.status:
        print(json.dumps(load_state(args.state_file), indent=2, sort_keys=True))
        return EXIT_QUIET

    # BEFORE the poll, unlike the sibling: `watched_s` is an input to `classify`
    # rather than something applied to its output, so one call fully determines
    # a state and the grace transition is testable as a pure function.
    prior = load_state(args.state_file)
    now = time.time()
    watched_s = max(0.0, now - prior.get("first_seen_at", now))

    status, body = fetch(args.url, args.timeout)
    verdict = classify(
        status,
        body,
        silence_s=args.silence_threshold,
        grace_s=args.grace,
        watched_s=watched_s,
    )
    state, message = decide(verdict, prior, now)

    # Always visible, even when silent — `never-posted` must never be something
    # a reader has to go looking for.
    print(f"{verdict.state}: {verdict.detail}")

    if message is None:
        save_state(args.state_file, state)
        return EXIT_QUIET

    delivered = args.notify_command is None or notify(args.notify_command, message)
    if not delivered:
        # A monitor cannot page about its own paging failure. What it CAN do is
        # leave `announced` where it was, so the next poll re-derives the same
        # message and tries again, and exit nonzero so the supervisor logs a
        # failed unit.
        state["notify_failed_at"] = time.time()
        save_state(args.state_file, state)
        print(f"NOTIFY FAILED, message not delivered: {message}", file=sys.stderr)
        return EXIT_NOTIFY_FAILED

    save_state(args.state_file, announce(state, verdict, time.time()))
    print(message)
    return EXIT_SPOKE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
