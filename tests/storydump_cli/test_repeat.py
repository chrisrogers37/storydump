"""A repeating story from the terminal (#485): ``schedule`` and
``reschedule``'s ``--repeat``, ``--every``, ``--on`` and ``--no-repeat``,
``cancel --stop-repeat``, and the ``repeats`` column ``planned`` shows.

What is pinned: each option reaches the body as the port reads it, and
nothing is computed here (no date, no bound); a rule that cannot be one is a
usage error before anything is sent; the port's refusal of a rule in the
verb's own words; ``cancel``'s key stays the web's whatever it carries; and
the rule in ``vocabulary.describe_repeat``'s words, in the answer and the
read.
"""

from __future__ import annotations

import pytest

from src.services.target.vocabulary import (
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_USAGE,
    REPEAT_SENTENCES,
)
from storydump_cli.commands import writes
from tests.storydump_cli.test_main import one_envelope, run
from tests.storydump_cli.test_planning import (
    ACCOUNT,
    AT,
    ITEM,
    ROW,
    SCHEDULED,
    STORY,
    queue,
    reschedule,
    schedule,
)
from tests.storydump_cli.test_writes import (
    WS,
    body_of,
    posts,
    route,
    write_api,
    write_runtime,
)

WEEKLY = {"unit": "week", "every": 1, "days": [1, 4], "at": "18:30"}


# --- schedule ------------------------------------------------------------------


def test_schedule_sends_the_rule_beside_the_time(tmp_path):
    api, result = schedule(
        tmp_path,
        (200, {**SCHEDULED, "repeat": WEEKLY}),
        "--repeat",
        "week",
        "--on",
        "mon,thu",
    )
    assert result.exit_code == EXIT_OK, result.output
    (request,) = posts(api)
    assert body_of(request) == {
        "media_item_id": ITEM,
        "local_at": AT,
        "ig_account_id": ACCOUNT,
        "repeat": {"unit": "week", "days": [1, 4]},
    }, "no every unless asked, no default day: the port fills both"
    assert "repeats every week on Mon and Thu at 18:30" in result.output


def test_every_is_sent_as_given_and_its_bounds_are_the_ports(tmp_path):
    api, result = schedule(
        tmp_path, (200, SCHEDULED), "--repeat", "day", "--every", "400"
    )
    assert result.exit_code == EXIT_OK, result.output
    (request,) = posts(api)
    assert body_of(request)["repeat"] == {"unit": "day", "every": 400}


def test_no_rule_sends_no_repeat(tmp_path):
    api, _ = schedule(tmp_path)
    (request,) = posts(api)
    assert "repeat" not in body_of(request)


@pytest.mark.parametrize(
    "extra",
    [
        ("--every", "2"),
        ("--on", "mon"),
        ("--repeat", "day", "--on", "mon"),
        ("--repeat", "week", "--on", "mon,funday"),
        ("--repeat", "week", "--on", ","),
        ("--repeat", "month"),
    ],
    ids=" ".join,
)
def test_what_cannot_be_a_rule_is_a_usage_error_and_nothing_is_sent(tmp_path, extra):
    api, result = schedule(tmp_path, (200, SCHEDULED), *extra)
    assert result.exit_code == EXIT_USAGE, result.output
    assert posts(api) == []


def test_a_rule_the_ledger_refuses_says_what_a_rule_takes(tmp_path):
    api, result = schedule(
        tmp_path,
        (
            400,
            {
                "reason": "invalid_args",
                "detail": "the repeat rule is not valid (ck_intent_repeat)",
                "facts": {"repeat": "invalid"},
            },
        ),
        "--repeat",
        "week",
        "--every",
        "60",
        "--json",
    )
    error = one_envelope(result)["error"]
    assert result.exit_code == EXIT_REFUSED
    assert error["detail"] == REPEAT_SENTENCES["invalid"]
    assert error["fix"] == writes.REPEAT_FIX


def test_a_day_off_the_series_days_says_how_to_put_it_on_one(tmp_path):
    api, result = schedule(
        tmp_path,
        (
            400,
            {
                "reason": "invalid_args",
                "detail": "the story falls on Wed, which is not one of the series' days",
                "facts": {"repeat": "off_day"},
            },
        ),
        "--repeat",
        "week",
        "--on",
        "mon,thu",
        "--json",
    )
    error = one_envelope(result)["error"]
    assert result.exit_code == EXIT_REFUSED
    assert error["detail"] == REPEAT_SENTENCES["off_day"]
    assert error["fix"] == writes.REPEAT_FIXES["off_day"]


# --- reschedule ------------------------------------------------------------------


def test_a_rule_alone_sends_no_time(tmp_path):
    api, result = reschedule(
        tmp_path,
        "--repeat",
        "day",
        "--every",
        "2",
        answer=(
            200,
            {
                **SCHEDULED,
                "local_at": None,
                "repeat": {**WEEKLY, "unit": "day", "every": 2, "days": None},
            },
        ),
    )
    assert result.exit_code == EXIT_OK, result.output
    (request,) = posts(api)
    assert body_of(request) == {
        "intent_id": STORY,
        "repeat": {"unit": "day", "every": 2},
    }
    assert "repeats every 2 days at 18:30" in result.output


def test_no_repeat_sends_null_and_the_answer_says_the_series_stopped(tmp_path):
    api, result = reschedule(
        tmp_path,
        "--no-repeat",
        answer=(200, {**SCHEDULED, "repeat": None, "repeat_stopped": True}),
    )
    assert result.exit_code == EXIT_OK, result.output
    (request,) = posts(api)
    assert body_of(request) == {"intent_id": STORY, "repeat": None}
    assert "its repeat has stopped" in result.output


def test_a_stop_of_a_story_that_never_repeated_says_nothing_of_a_series(tmp_path):
    """The line is the answer's: a story with no rule had nothing to stop."""
    api, result = reschedule(
        tmp_path,
        "--no-repeat",
        answer=(200, {**SCHEDULED, "repeat": None, "repeat_stopped": False}),
    )
    assert result.exit_code == EXIT_OK, result.output
    assert "repeat" not in result.output


def test_a_time_and_a_stop_go_together(tmp_path):
    api, result = reschedule(tmp_path, "--at", AT, "--no-repeat")
    assert result.exit_code == EXIT_OK, result.output
    (request,) = posts(api)
    assert body_of(request) == {"intent_id": STORY, "local_at": AT, "repeat": None}


@pytest.mark.parametrize(
    "extra",
    [(), ("--no-repeat", "--repeat", "day"), ("--no-repeat", "--every", "2")],
    ids=lambda e: " ".join(e) or "nothing",
)
def test_reschedule_needs_one_thing_to_change(tmp_path, extra):
    api, result = reschedule(tmp_path, *extra)
    assert result.exit_code == EXIT_USAGE, result.output
    assert posts(api) == []


# --- cancel -----------------------------------------------------------------------


def test_cancel_stop_repeat_is_its_own_request_under_its_own_key(tmp_path):
    """A plain cancel and one that also ends the series are two requests, as a
    resolution and its `--not-posted` retry are: `cancel:<story>:stop_repeat`.
    Under one key the second would be refused as a conflict, never answered."""
    api = write_api(
        {
            route(WS, "cancel"): (
                200,
                {
                    "outcome": "executed",
                    "intent_id": STORY,
                    "state": "scheduled",
                    "cancel_requested": True,
                    "repeat_stopped": True,
                },
            )
        }
    )
    result = run(
        write_runtime(tmp_path, api),
        "cancel",
        STORY,
        "--stop-repeat",
        "--workspace",
        WS,
    )
    assert result.exit_code == EXIT_OK, result.output
    (request,) = posts(api)
    assert request.headers["Idempotency-Key"] == f"cancel:{STORY}:stop_repeat"
    assert body_of(request) == {"intent_id": STORY, "stop_repeat": True}
    assert "its repeat has stopped" in result.output


# --- planned ----------------------------------------------------------------------


def test_planned_shows_how_a_story_repeats(tmp_path):
    api = write_api(
        queue(WS, [{**ROW, "repeat": WEEKLY}, {**ROW, "id": "x", "repeat": None}])
    )
    result = run(write_runtime(tmp_path, api), "planned", "--workspace", WS)
    assert result.exit_code == EXIT_OK, result.output
    assert "every week on Mon and Thu at 18:30" in " ".join(result.output.split())


def test_planned_shows_a_rule_it_cannot_read_as_given(tmp_path):
    """A rule from an API newer than this CLI is shown, never a crash."""
    odd = {"unit": "month", "every": 1, "days": None, "at": "18:30"}
    api = write_api(queue(WS, [{**ROW, "repeat": odd}]))
    result = run(write_runtime(tmp_path, api), "planned", "--workspace", WS)
    assert result.exit_code == EXIT_OK, result.output
    assert "unit=month" in result.output
