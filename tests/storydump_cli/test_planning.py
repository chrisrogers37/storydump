"""Planning a story from the terminal (#1413 phase 5): ``schedule``,
``reschedule`` and ``planned``.

The two writes are ONE call each to the command port, like every write verb,
under a FRESH key per invocation: planning an item again after a cancel, or
moving a story back to a time it had, is a new act, and a duplicate schedule
is the database's to refuse. ``planned`` is the web's Queue read filtered to
planned stories — no endpoint of its own.

What is pinned: the route, the key and the body; an account named by handle
resolves through the account view to the one LIVE account it names before
anything is sent; each refusal in the verb's own words, chosen from the
refusal's facts, never its prose (a lock names what is in the way and whether
``--override-locks`` gets past it; a time names the rule it broke; a
``not_found`` names what is missing) under the vocabulary's exit code; the
answer's due time and its warnings; the read's filter and its closed states.
"""

from __future__ import annotations

import pytest

from src.services.target.vocabulary import (
    AT_RULE_SENTENCES,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_USAGE,
    IN_THE_WAY,
    MISSING_SENTENCES,
    NO_PUSH_BINDING,
    WARNING_SENTENCES,
)
from storydump_cli.commands import writes
from tests.storydump_cli.test_main import one_envelope, run
from tests.storydump_cli.test_reads import path, view
from tests.storydump_cli.test_writes import (
    TWO,
    UUID,
    WS,
    WS_B,
    body_of,
    posts,
    route,
    write_api,
    write_runtime,
)

ITEM = "3c6e0b8a-9d7f-4a1e-b2c3-4d5e6f7a8b9c"
ACCOUNT = "7b7b7b7b-7b7b-4b7b-8b7b-7b7b7b7b7b7b"
OTHER_ACCOUNT = "8c8c8c8c-8c8c-4c8c-8c8c-8c8c8c8c8c8c"
STORY = "0395b173-9c3e-4a6b-8f2e-6d1c2b3a4f50"
AT = "2026-10-12 18:30"
HANDLE = "storydump.studio"

SCHEDULED = {
    "outcome": "executed",
    "intent_id": STORY,
    "state": "scheduled",
    "schedule_slot_at": "2026-10-12T22:30:00+00:00",
    "tz": "America/New_York",
    "local_at": "2026-10-12 18:30:00",
    "overridden": [],
    "warnings": [],
}


def accounts(*rows) -> dict:
    """The account view's answer for HANDLE in WS."""
    return {("GET", path("account", WS, HANDLE)): view("account", WS, list(rows))}


def schedule(tmp_path, answer=(200, SCHEDULED), *extra, account=ACCOUNT, routes=None):
    api = write_api({route(WS, "schedule_item"): answer, **(routes or {})})
    result = run(
        write_runtime(tmp_path, api),
        "schedule",
        ITEM,
        "--account",
        account,
        "--at",
        AT,
        "--workspace",
        WS,
        *extra,
    )
    return api, result


def refused(tmp_path, body: dict, status: int = 409, verb="schedule"):
    """The error envelope a refusal of *verb* renders."""
    if verb == "schedule":
        api, result = schedule(tmp_path, (status, body), "--json")
    else:
        api = write_api({route(WS, "reschedule_item"): (status, body)})
        result = run(
            write_runtime(tmp_path, api),
            "reschedule",
            STORY,
            "--at",
            AT,
            "--workspace",
            WS,
            "--json",
        )
    return result, one_envelope(result)["error"]


# --- schedule ------------------------------------------------------------------


def test_schedule_sends_the_item_the_account_and_the_wall_time_under_a_fresh_key(
    tmp_path,
):
    api, result = schedule(tmp_path)
    assert result.exit_code == EXIT_OK, result.output
    (request,) = posts(api)
    assert request.url.path == f"/api/v1/workspaces/{WS}/commands/schedule_item"
    assert request.headers["Idempotency-Key"] == f"schedule_item:{WS}:{UUID}"
    assert body_of(request) == {
        "media_item_id": ITEM,
        "local_at": AT,
        "ig_account_id": ACCOUNT,
    }
    # an account given by id is sent as given: no lookup
    assert not [p for p in api.paths("GET") if "/account/" in p]
    assert (
        f"scheduled — story {STORY} (now scheduled),"
        f" due 2026-10-12 18:30:00 America/New_York"
    ) in result.output


def test_a_handle_resolves_to_its_one_live_account(tmp_path):
    """A removed or moved destination keeps its handle, so a handle can name
    several rows; the live one is the account."""
    api, result = schedule(
        tmp_path,
        account=HANDLE,
        routes=accounts(
            {"id": OTHER_ACCOUNT, "handle": HANDLE, "state": "disabled"},
            {"id": ACCOUNT, "handle": HANDLE, "state": "reauth_required"},
        ),
    )
    assert result.exit_code == EXIT_OK, result.output
    (request,) = posts(api)
    assert body_of(request)["ig_account_id"] == ACCOUNT


def test_a_handle_with_no_live_account_is_not_found_and_nothing_is_sent(tmp_path):
    api, result = schedule(
        tmp_path,
        (200, SCHEDULED),
        "--json",
        account=HANDLE,
        routes=accounts({"id": ACCOUNT, "handle": HANDLE, "state": "moved"}),
    )
    assert result.exit_code == EXIT_NOT_FOUND
    error = one_envelope(result)["error"]
    assert error["reason"] == "not_found" and HANDLE in error["detail"]
    assert posts(api) == []


def test_a_handle_on_an_api_older_than_the_cli_is_left_to_the_port(tmp_path):
    """An account view that carries no `state` comes from an API older than
    this CLI and cannot say which row is live: its one row is sent, so the
    answer is the API's own, never a false "no live account"."""
    api, result = schedule(
        tmp_path, account=HANDLE, routes=accounts({"id": ACCOUNT, "handle": HANDLE})
    )
    assert result.exit_code == EXIT_OK, result.output
    assert body_of(posts(api)[0])["ig_account_id"] == ACCOUNT


def test_a_blank_account_is_a_usage_error_and_nothing_is_sent(tmp_path):
    api, result = schedule(tmp_path, account="  ")
    assert result.exit_code == EXIT_USAGE
    assert "--account" in result.output
    assert posts(api) == [] and api.paths("GET") == []


def test_a_handle_naming_two_live_accounts_needs_the_id(tmp_path):
    api, result = schedule(
        tmp_path,
        (200, SCHEDULED),
        "--json",
        account=HANDLE,
        routes=accounts(
            {"id": ACCOUNT, "handle": HANDLE, "state": "active"},
            {"id": OTHER_ACCOUNT, "handle": HANDLE, "state": "active"},
        ),
    )
    assert result.exit_code == EXIT_USAGE
    assert "2 live accounts" in one_envelope(result)["error"]["detail"]
    assert posts(api) == []


def test_override_locks_is_sent_only_when_asked(tmp_path):
    api, _ = schedule(tmp_path)
    assert "override_locks" not in body_of(posts(api)[0])
    api, result = schedule(tmp_path, (200, SCHEDULED), "--override-locks")
    assert result.exit_code == EXIT_OK, result.output
    assert body_of(posts(api)[0])["override_locks"] is True


def test_a_held_back_item_names_the_lock_and_the_override(tmp_path):
    result, error = refused(
        tmp_path,
        {
            "reason": "locked",
            "detail": "command refused: locked — …",
            "facts": {"in_the_way": ["skip"], "overridable": True},
        },
    )
    assert result.exit_code == EXIT_REFUSED
    assert error["reason"] == "locked"
    assert error["detail"] == f"this item is held back: {IN_THE_WAY['skip']}"
    assert error["fix"] == "run it again with --override-locks to schedule it anyway"


def test_a_blocked_item_says_the_override_does_not_help(tmp_path):
    result, error = refused(
        tmp_path,
        {
            "reason": "locked",
            "detail": "command refused: locked — …",
            "facts": {"in_the_way": ["reject", "skip"], "overridable": False},
        },
    )
    assert result.exit_code == EXIT_REFUSED
    assert error["detail"] == (
        f"this item cannot be scheduled: {IN_THE_WAY['reject']}; {IN_THE_WAY['skip']}"
    )
    assert "does not get past" in error["fix"]


@pytest.mark.parametrize("missing", sorted(MISSING_SENTENCES))
def test_a_not_found_names_what_is_missing(tmp_path, missing):
    result, error = refused(
        tmp_path,
        {
            "reason": "not_found",
            "detail": "command refused: …",
            "facts": {"missing": missing},
        },
        404,
    )
    assert result.exit_code == EXIT_NOT_FOUND
    assert error["reason"] == "not_found"
    assert error["detail"] == MISSING_SENTENCES[missing]
    # where to look, by what is missing: an item's id is never on the web
    assert (
        error["fix"]
        == {
            "account": "storydump account <handle> shows an account",
            "item": "check the item's id: storydump story <story> shows a story's item"
            " as media",
        }[missing]
    )


def test_every_missing_thing_has_its_own_fix():
    assert set(writes.MISSING_FIXES) == set(MISSING_SENTENCES)


@pytest.mark.parametrize(
    ("origin", "whose"),
    [
        # a re-run after an answer that never arrived meets the person's own story
        ("planned", "if you just ran this, it is the story you planned; otherwise "),
        ("cadence", "the cadence picked that item for that account; "),
    ],
)
def test_an_item_already_waiting_names_the_story_in_the_way(tmp_path, origin, whose):
    """Whatever its origin or state, the story holding the item is the one to
    look at: the refusal names it, so the fix is that story's own view."""
    result, error = refused(
        tmp_path,
        {
            "reason": "illegal_transition",
            "detail": "command refused: …",
            "facts": {
                "existing": {
                    "intent_id": STORY,
                    "state": "awaiting_approval",
                    "origin": origin,
                }
            },
        },
    )
    assert result.exit_code == EXIT_REFUSED
    assert error["detail"] == (
        f"that item already waits on that account: story {STORY}"
        f" ({origin}, awaiting_approval)"
    )
    assert error["fix"] == (
        f"storydump story {STORY} shows it: {whose}cancel it to plan the item again"
    )


def test_an_item_whose_story_is_being_cancelled_says_to_wait_for_it(tmp_path):
    result, error = refused(
        tmp_path,
        {
            "reason": "illegal_transition",
            "detail": "command refused: …",
            "facts": {
                "existing": {
                    "intent_id": STORY,
                    "state": "scheduled",
                    "origin": "planned",
                    "cancel_requested": True,
                }
            },
        },
    )
    assert result.exit_code == EXIT_REFUSED
    assert error["detail"] == (
        f"that item's story on that account, {STORY}, is still being cancelled"
    )
    assert error["fix"] == (
        f"run it again once the cancel has landed (storydump story {STORY} shows it)"
    )


def test_an_item_already_waiting_with_no_story_named_says_where_to_look(tmp_path):
    result, error = refused(
        tmp_path, {"reason": "illegal_transition", "detail": "command refused: …"}
    )
    assert result.exit_code == EXIT_REFUSED
    assert "already waiting to post" in error["detail"]
    assert error["fix"] == (
        "run it again, since the story in the way may have just ended; if it is"
        " refused again, storydump account <handle> shows that account's stories"
    )


@pytest.mark.parametrize("verb", ["schedule", "reschedule"])
@pytest.mark.parametrize("rule", sorted(AT_RULE_SENTENCES))
def test_a_refused_time_says_which_rule_it_broke(tmp_path, verb, rule):
    """The rule comes from the refusal's facts, never its prose; the shared
    sentence would say only that the arguments were refused."""
    result, error = refused(
        tmp_path,
        {
            "reason": "invalid_args",
            "detail": "command refused: …",
            "facts": {"at_rule": rule},
        },
        400,
        verb=verb,
    )
    assert result.exit_code == EXIT_REFUSED
    assert error["detail"] == AT_RULE_SENTENCES[rule] and error["fix"] == writes.AT_FIX


def test_a_refusal_with_no_rule_keeps_the_shared_words(tmp_path):
    result, error = refused(
        tmp_path, {"reason": "invalid_args", "detail": "command refused: …"}, 400
    )
    assert result.exit_code == EXIT_REFUSED
    assert error["fix"] != writes.AT_FIX


def test_the_answer_says_what_it_overrode_and_what_it_warns_of(tmp_path):
    _, result = schedule(
        tmp_path,
        (
            200,
            {**SCHEDULED, "overridden": ["skip"], "warnings": [NO_PUSH_BINDING]},
        ),
        "--override-locks",
    )
    assert result.exit_code == EXIT_OK, result.output
    assert "scheduled over: skip" in result.output
    assert WARNING_SENTENCES[NO_PUSH_BINDING] in result.output


def test_each_run_is_a_new_attempt(tmp_path):
    """The same schedule twice is two keys: after a cancel, planning the item
    again at the same time must not replay the first answer."""
    uuids = iter(["11111111-1111-4111-8111-111111111111", UUID])
    api = write_api({route(WS, "schedule_item"): (200, SCHEDULED)})
    rt = write_runtime(tmp_path, api)
    rt.uuid_fn = lambda: next(uuids)
    for _ in range(2):
        run(rt, "schedule", ITEM, "--account", ACCOUNT, "--at", AT, "--workspace", WS)
    first, second = (r.headers["Idempotency-Key"] for r in posts(api))
    assert first != second


# --- reschedule ------------------------------------------------------------------


def test_reschedule_sends_the_story_and_the_time_under_a_fresh_key(tmp_path):
    api = write_api(
        {
            route(WS, "reschedule_item"): (
                200,
                {**SCHEDULED, "previous_slot_at": "2026-10-11T22:30:00+00:00"},
            )
        }
    )
    result = run(
        write_runtime(tmp_path, api), "reschedule", STORY, "--at", AT, "--workspace", WS
    )
    assert result.exit_code == EXIT_OK, result.output
    (request,) = posts(api)
    assert request.headers["Idempotency-Key"] == f"reschedule_item:{WS}:{UUID}"
    assert body_of(request) == {"intent_id": STORY, "local_at": AT}
    assert "rescheduled — story" in result.output
    assert "due 2026-10-12 18:30:00 America/New_York" in result.output


def test_a_story_that_can_no_longer_move_says_so(tmp_path):
    result, error = refused(
        tmp_path,
        {"reason": "illegal_transition", "detail": "command refused: …"},
        verb="reschedule",
    )
    assert result.exit_code == EXIT_REFUSED
    assert "still waiting for its time" in error["detail"]


# --- planned ----------------------------------------------------------------------

ROW = {
    "id": STORY,
    "state": "scheduled",
    "cancel_requested": False,
    "schedule_slot_at": "2026-10-12T22:30:00+00:00",
    "account_handle": HANDLE,
    "file_name": "sunset.jpg",
    "scheduled_by": "Chris",
    "origin": "planned",
    "tz": "America/New_York",
}


def queue(ws: str, rows: list, limit: int = 50) -> dict:
    """The Queue read's answer, echoing the page size as the API does."""
    return {
        ("GET", f"/api/v1/workspaces/{ws}/intents"): (
            200,
            {"intents": rows, "limit": limit},
        )
    }


def test_planned_is_the_queue_read_filtered_to_planned_stories(tmp_path):
    api = write_api(queue(WS, [ROW]))
    result = run(write_runtime(tmp_path, api), "planned", "--workspace", WS)
    assert result.exit_code == EXIT_OK, result.output
    (request,) = [r for r in api.calls if r.url.path.endswith("/intents")]
    assert dict(request.url.params) == {"origin": "planned", "state": "scheduled"}
    for cell in ("sunset.jpg", HANDLE, "Chris"):
        assert cell in result.output
    # due in the zone its time was chosen in, the time the person typed
    assert "2026-10-12 18:30 America/New_York" in result.output
    assert "a full page" not in result.output, "a short page is the whole list"


@pytest.mark.parametrize("tz", [None, "Not/AZone"])
def test_planned_shows_the_time_as_given_where_it_cannot_name_the_zone(tmp_path, tz):
    api = write_api(queue(WS, [{**ROW, "tz": tz}]))
    result = run(write_runtime(tmp_path, api), "planned", "--workspace", WS)
    assert result.exit_code == EXIT_OK, result.output
    assert "2026-10-12T22:30:00+00:00" in result.output


def test_planned_shows_the_time_as_given_where_the_zone_cannot_be_read(
    tmp_path, monkeypatch
):
    """With the `tzdata` package installed, a key that is a directory there
    raises an OSError rather than ZoneInfoNotFoundError; the list shows the
    time as given all the same."""
    from storydump_cli import output

    def unreadable(key):
        raise IsADirectoryError(key)

    monkeypatch.setattr(output, "ZoneInfo", unreadable)
    api = write_api(queue(WS, [ROW]))
    result = run(write_runtime(tmp_path, api), "planned", "--workspace", WS)
    assert result.exit_code == EXIT_OK, result.output
    assert "2026-10-12T22:30:00+00:00" in result.output


def test_planned_shows_a_time_that_names_no_instant_as_given(tmp_path):
    api = write_api(queue(WS, [{**ROW, "schedule_slot_at": "2026-10-12T22:30:00"}]))
    result = run(write_runtime(tmp_path, api), "planned", "--workspace", WS)
    assert result.exit_code == EXIT_OK, result.output
    assert "2026-10-12T22:30:00" in result.output
    assert "America/New_York" not in result.output


def test_a_full_page_says_it_is_the_first(tmp_path):
    api = write_api(queue(WS, [ROW], limit=1))
    result = run(write_runtime(tmp_path, api), "planned", "--workspace", WS)
    assert result.exit_code == EXIT_OK, result.output
    assert (
        f"workspace {WS}: 1 shown, a full page — there may be more;"
        " pass --limit up to 200"
    ) in result.output


def test_a_full_page_at_the_most_one_read_returns_offers_no_wider_read(tmp_path):
    api = write_api(queue(WS, [ROW] * 200, limit=200))
    result = run(write_runtime(tmp_path, api), "planned", "--workspace", WS)
    assert result.exit_code == EXIT_OK, result.output
    assert (
        "200 shown, a full page — there may be more; 200 is the most one read returns"
    ) in result.output
    assert "--limit up to" not in result.output


def test_a_story_missed_at_its_time_says_why(tmp_path):
    missed = {**ROW, "state": "expired", "miss_reason": "late"}
    api = write_api(queue(WS, [missed]))
    result = run(
        write_runtime(tmp_path, api),
        "planned",
        "--workspace",
        WS,
        "--state",
        "expired",
    )
    assert result.exit_code == EXIT_OK, result.output
    assert "expired (late)" in result.output


def test_a_due_time_with_seconds_shows_them(tmp_path):
    api = write_api(
        queue(WS, [{**ROW, "schedule_slot_at": "2026-10-12T22:30:05+00:00"}])
    )
    result = run(write_runtime(tmp_path, api), "planned", "--workspace", WS)
    assert "2026-10-12 18:30:05 America/New_York" in result.output


def test_planned_says_a_cancel_still_landing(tmp_path):
    api = write_api(queue(WS, [{**ROW, "cancel_requested": True}]))
    result = run(write_runtime(tmp_path, api), "planned", "--workspace", WS)
    assert "scheduled (cancelling)" in result.output


def test_planned_says_nothing_of_a_cancel_that_landed(tmp_path):
    """The flag is never cleared, so a story that has ended still carries it."""
    ended = {**ROW, "state": "cancelled", "cancel_requested": True}
    api = write_api(queue(WS, [ended]))
    result = run(
        write_runtime(tmp_path, api),
        "planned",
        "--workspace",
        WS,
        "--state",
        "cancelled",
    )
    assert result.exit_code == EXIT_OK, result.output
    assert "cancelled" in result.output
    assert "(cancelling)" not in result.output


def test_planned_widens_by_state_and_bounds_the_page(tmp_path):
    api = write_api(queue(WS, [], limit=20))
    result = run(
        write_runtime(tmp_path, api),
        "planned",
        "--workspace",
        WS,
        "--state",
        "scheduled, awaiting_approval",
        "--limit",
        "20",
        "--json",
    )
    assert result.exit_code == EXIT_OK, result.output
    (request,) = [r for r in api.calls if r.url.path.endswith("/intents")]
    assert dict(request.url.params) == {
        "origin": "planned",
        "state": "scheduled,awaiting_approval",
        "limit": "20",
    }
    document = one_envelope(result)
    assert document["kind"] == "planned"
    assert document["data"] == {
        "workspaces": [{"workspace_id": WS, "rows": [], "limit": 20}]
    }


def test_planned_reads_a_history_newest_first_when_asked(tmp_path):
    api = write_api(queue(WS, []))
    result = run(
        write_runtime(tmp_path, api),
        "planned",
        "--workspace",
        WS,
        "--state",
        "expired",
        "--newest-first",
    )
    assert result.exit_code == EXIT_OK, result.output
    (request,) = [r for r in api.calls if r.url.path.endswith("/intents")]
    assert dict(request.url.params) == {
        "origin": "planned",
        "state": "expired",
        "order": "desc",
    }


@pytest.mark.parametrize("states", ["someday", "scheduled,someday", ","])
def test_planned_states_are_the_closed_list(tmp_path, states):
    api = write_api(queue(WS, []))
    result = run(write_runtime(tmp_path, api), "planned", "--state", states)
    assert result.exit_code == EXIT_USAGE
    assert not [r for r in api.calls if r.url.path.endswith("/intents")]


def test_planned_reads_every_workspace_by_default_and_says_when_none_are(tmp_path):
    api = write_api({**queue(WS, []), **queue(WS_B, [ROW])}, principal=TWO)
    result = run(write_runtime(tmp_path, api), "planned")
    assert result.exit_code == EXIT_OK, result.output
    assert "nothing planned" in result.output and "sunset.jpg" in result.output
