"""Planning a story from the terminal (#1413 phase 5): ``schedule``,
``reschedule`` and ``planned``.

The two writes are ONE call each to the command port, like every write verb,
under a FRESH key per invocation: planning an item again after a cancel, or
moving a story back to a time it had, is a new act, and a duplicate schedule
is the database's to refuse. ``planned`` is the web's Queue read filtered to
planned stories — no endpoint of its own.

What is pinned: the route, the key and the body; an account named by handle
resolves through the account view before anything is sent; each refusal in
the verb's own words (a lock names what is in the way and whether
``--override-locks`` gets past it; a time names the rule it broke); the
answer's due time and its warnings; the read's filter and its closed states.
"""

from __future__ import annotations

import pytest

from src.services.target.vocabulary import (
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_USAGE,
    IN_THE_WAY,
    NO_PUSH_BINDING,
    WARNING_SENTENCES,
)
from storydump_cli.commands import writes
from tests.storydump_cli.test_main import one_envelope, run
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
STORY = "0395b173-9c3e-4a6b-8f2e-6d1c2b3a4f50"
AT = "2026-10-12 18:30"

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


def account_view(ws: str, key: str, rows: list) -> dict:
    return {
        ("GET", f"/api/v1/ops/workspaces/{ws}/account/{key}"): (
            200,
            {
                "v": 1,
                "kind": "account",
                "data": {"workspace_id": ws, "rows": rows},
                "error": None,
            },
        )
    }


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


def test_a_handle_resolves_through_the_account_view_first(tmp_path):
    api, result = schedule(
        tmp_path,
        account="storydump.studio",
        routes=account_view(WS, "storydump.studio", [{"id": ACCOUNT, "handle": "x"}]),
    )
    assert result.exit_code == EXIT_OK, result.output
    (request,) = posts(api)
    assert body_of(request)["ig_account_id"] == ACCOUNT


def test_an_unknown_handle_is_not_found_and_nothing_is_sent(tmp_path):
    api, result = schedule(
        tmp_path,
        (200, SCHEDULED),
        "--json",
        account="nobody",
        routes=account_view(WS, "nobody", []),
    )
    assert result.exit_code == EXIT_NOT_FOUND
    error = one_envelope(result)["error"]
    assert error["reason"] == "not_found" and "nobody" in error["detail"]
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
            "in_the_way": ["skip"],
            "overridable": True,
        },
    )
    assert result.exit_code == EXIT_REFUSED
    assert error["reason"] == "locked"
    assert IN_THE_WAY["skip"] in error["detail"]
    assert "--override-locks" in error["fix"] and "anyway" in error["fix"]


def test_a_blocked_item_says_the_override_does_not_help(tmp_path):
    result, error = refused(
        tmp_path,
        {
            "reason": "locked",
            "detail": "command refused: locked — …",
            "in_the_way": ["reject", "skip"],
            "overridable": False,
        },
    )
    assert result.exit_code == EXIT_REFUSED
    assert IN_THE_WAY["reject"] in error["detail"]
    assert IN_THE_WAY["skip"] in error["detail"]
    assert "does not get past" in error["fix"]


def test_every_kind_in_the_way_has_the_clis_words():
    """The port names these and the CLI says them: the item's two unpostable
    states, then every lock kind (`ck_locks_kind`, split by F7)."""
    from src.services.target.vocabulary import BLOCKING_LOCKS, WARNING_LOCKS

    assert set(IN_THE_WAY) == {
        "item_removed",
        "item_unsupported",
        *BLOCKING_LOCKS,
        *WARNING_LOCKS,
    }


@pytest.mark.parametrize(
    "reason,status,code,words",
    [
        ("not_found", 404, EXIT_NOT_FOUND, "no such item"),
        ("illegal_transition", 409, EXIT_REFUSED, "already waiting to post"),
    ],
)
def test_the_other_refusals_speak_of_items_and_accounts(
    tmp_path, reason, status, code, words
):
    result, error = refused(
        tmp_path, {"reason": reason, "detail": "command refused: …"}, status
    )
    assert result.exit_code == code
    assert error["reason"] == reason and words in error["detail"]


@pytest.mark.parametrize("verb", ["schedule", "reschedule"])
def test_a_refused_time_says_which_rule_it_broke(tmp_path, verb):
    """The port's words name the rule — a skipped wall time, the past, the
    horizon — and the CLI's shared sentence would say only "refused"."""
    detail = (
        "command refused: invalid_args — 2027-03-14 02:30:00 does not happen in"
        " America/New_York: the clocks skip it"
    )
    result, error = refused(
        tmp_path, {"reason": "invalid_args", "detail": detail}, 400, verb=verb
    )
    assert result.exit_code == EXIT_REFUSED
    assert error["detail"] == detail and error["fix"] == writes.AT_FIX


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


def test_every_warning_the_port_gives_has_the_clis_words():
    assert set(WARNING_SENTENCES) == {NO_PUSH_BINDING}


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


def test_the_verbs_map_to_the_ports_commands():
    assert writes.COMMAND_OF["schedule"] == "schedule_item"
    assert writes.COMMAND_OF["reschedule"] == "reschedule_item"


# --- planned ----------------------------------------------------------------------

ROW = {
    "id": STORY,
    "state": "scheduled",
    "cancel_requested": False,
    "schedule_slot_at": "2026-10-12T22:30:00+00:00",
    "account_handle": "storydump.studio",
    "file_name": "sunset.jpg",
    "scheduled_by": "Chris",
    "origin": "planned",
}


def queue(ws: str, rows: list) -> dict:
    return {
        ("GET", f"/api/v1/workspaces/{ws}/intents"): (
            200,
            {"intents": rows, "limit": 50},
        )
    }


def test_planned_is_the_queue_read_filtered_to_planned_stories(tmp_path):
    api = write_api(queue(WS, [ROW]))
    result = run(write_runtime(tmp_path, api), "planned", "--workspace", WS)
    assert result.exit_code == EXIT_OK, result.output
    (request,) = [r for r in api.calls if r.url.path.endswith("/intents")]
    assert dict(request.url.params) == {"origin": "planned", "state": "scheduled"}
    for cell in ("sunset.jpg", "storydump.studio", "Chris", "2026-10-12T22:30:00"):
        assert cell in result.output


def test_planned_says_a_cancel_still_landing(tmp_path):
    api = write_api(queue(WS, [{**ROW, "cancel_requested": True}]))
    result = run(write_runtime(tmp_path, api), "planned", "--workspace", WS)
    assert "scheduled (cancelling)" in result.output


def test_planned_widens_by_state_and_bounds_the_page(tmp_path):
    api = write_api(queue(WS, []))
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
    assert document["data"] == {"workspaces": [{"workspace_id": WS, "rows": []}]}


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
