"""The read verbs against a scripted API.

What is pinned: the loop over every workspace the principal lists lands in
ONE envelope whose ``data`` is always ``{"workspaces": [{"workspace_id",
"rows"}]}``; ``--workspace`` narrows it by id or by exact name; a key that
resolves to nothing in every workspace is exit 1 with the CLI's sentence; a
workspace the principal cannot see (a 404) is exit 3 and an API that did
not answer is exit 4, both through the mapping ``main`` already owns;
``--since`` takes ``Nm``/``Nh``/``Nd`` or ISO-8601 and always sends UTC; a
token inside a row never reaches the terminal in either mode; and every
verb's help carries one Example.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from src.services.target.vocabulary import (
    EXIT_API_UNREACHABLE,
    EXIT_NOT_AUTHORIZED,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_USAGE,
    REASON_SENTENCES,
    check_envelope,
)
from tests.storydump_cli.test_main import (
    PERSON,
    SECRET,
    USER,
    WS,
    Api,
    one_envelope,
    run,
    runtime,
)

WS_A = WS
WS_B = "55555555-5555-4555-8555-555555555555"
NAME_A = "Chris's studio"
NAME_B = "Second studio"
INTENT = "66666666-6666-4666-8666-666666666666"
OTHER_INTENT = "99999999-9999-4999-8999-999999999999"
ACCOUNT_ID = "77777777-7777-4777-8777-777777777777"
MEDIA_ID = "88888888-8888-4888-8888-888888888888"
BINDING = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
JOB_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
NOW = datetime(2026, 9, 15, 15, 0, tzinfo=timezone.utc)

TWO = {
    **PERSON,
    "workspaces": [
        {"id": WS_A, "name": NAME_A, "role": "owner"},
        {"id": WS_B, "name": NAME_B, "role": "member"},
    ],
}

VIEWS = ("story", "cards", "floating", "account", "jobs", "outbox", "burst")


# --- the scripted API ---------------------------------------------------------


def path(view: str, ws: str, key: str | None = None) -> str:
    base = f"/api/v1/ops/workspaces/{ws}/{view}"
    return f"{base}/{key}" if key else base


def view(kind: str, ws: str, rows: list) -> tuple[int, dict]:
    """The API's answer for one workspace: the phase-01 envelope."""
    return (
        200,
        {
            "v": 1,
            "kind": kind,
            "data": {"workspace_id": ws, "rows": rows},
            "error": None,
        },
    )


def reads_api(routes=None, principal=TWO) -> Api:
    api = Api({("GET", "/api/v1/me/principal"): (200, principal)})
    api.routes.update(routes or {})
    return api


def read_runtime(tmp_path, api: Api, **kw):
    rt = runtime(tmp_path, api, **kw)
    rt.now_fn = lambda: NOW
    return rt


def ops_calls(api: Api) -> list[tuple[str, dict[str, str]]]:
    return [
        (r.url.path, dict(r.url.params)) for r in api.calls if "/ops/" in r.url.path
    ]


def workspaces_of(document) -> dict[str, list]:
    data = document["data"]
    assert list(data.keys()) == ["workspaces"], data
    out = {}
    for entry in data["workspaces"]:
        assert list(entry.keys()) == ["workspace_id", "rows"], entry
        out[entry["workspace_id"]] = entry["rows"]
    return out


# --- rows ---------------------------------------------------------------------


def floating_row(ws: str, intent_id: str = INTENT, **over) -> dict:
    row = {
        "workspace_id": ws,
        "id": intent_id,
        "publish_step": "container_created",
        "attempts_by_step": {"container_created": 1},
        "cap_consumed_on": "2026-09-15",
        "entered_state_at": "2026-09-15T14:50:00Z",
        "job_state": "ready",
        "job_run_at": "2026-09-15T15:01:00Z",
        "job_attempts": 1,
        "last_wait_class": "container_not_ready",
        "last_wait_rung": 2,
        "last_wait_at": "2026-09-15T14:55:00Z",
    }
    row.update(over)
    return row


def story_row(ws: str, intent_id: str = INTENT, **intent_over) -> dict:
    intent = {
        "id": intent_id,
        "state": "approved",
        "publish_step": "container_created",
        "cap_consumed_on": "2026-09-15",
        "attempts_by_step": {"container_created": 1},
        "entered_state_at": "2026-09-15T14:50:00Z",
        "schedule_slot_at": "2026-09-15T14:45:00Z",
        "ig_account_id": ACCOUNT_ID,
        "media_item_id": MEDIA_ID,
        "last_error": None,
    }
    intent.update(intent_over)
    return {
        "workspace_id": ws,
        "intent": intent,
        "audit": [
            {
                "at": "2026-09-15T14:50:00Z",
                "from_state": "awaiting_approval",
                "to_state": "approved",
                "actor_kind": "user",
                "actor_user_id": USER,
                "channel": "telegram",
                "detail": {"event": "approve"},
            }
        ],
        "operations": [
            {
                "at": "2026-09-15T14:50:30Z",
                "op_kind": "create_container",
                "generation": 1,
                "state": "permitted",
                "url_variant": "fresh",
                "error": None,
                "subcode": None,
                "elapsed_ms": 812,
            }
        ],
        "cards": [
            {
                "at": "2026-09-15T14:40:00Z",
                "binding_id": BINDING,
                "kind": "approval",
                "state": "sent",
                "external_message_ref": "1234",
                "attempts": 1,
                "outcome_text": None,
            }
        ],
    }


def cards_row(ws: str, **over) -> dict:
    row = {
        "workspace_id": ws,
        "id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        "binding_id": BINDING,
        "channel": "telegram",
        "external_ref": "-1001",
        "kind": "approval",
        "state": "sent",
        "external_message_ref": "1234",
        "attempts": 1,
        "outcome_text": None,
        "created_at": "2026-09-15T14:40:00Z",
        "updated_at": "2026-09-15T14:40:05Z",
    }
    row.update(over)
    return row


def account_row(ws: str, **over) -> dict:
    row = {
        "workspace_id": ws,
        "id": ACCOUNT_ID,
        "handle": "storydump.studio",
        "posts_per_day": 3,
        "tz": "America/New_York",
        "next_slot_at": "2026-09-15T18:00:00Z",
        "today": {"local_date": "2026-09-15", "count": 2, "cap_at_write": 3},
        "recent": [
            {
                "id": INTENT,
                "state": "posted",
                "entered_state_at": "2026-09-15T14:52:00Z",
            }
        ],
    }
    row.update(over)
    return row


def jobs_row(ws: str, **over) -> dict:
    row = {
        "workspace_id": ws,
        "kind": "publish",
        "lane": "publish",
        "state": "ready",
        "count": 2,
        "oldest_run_at": "2026-09-15T14:59:00Z",
        "samples": [],
    }
    row.update(over)
    return row


def outbox_row(ws: str, **over) -> dict:
    row = {
        "workspace_id": ws,
        "binding_id": BINDING,
        "channel": "telegram",
        "external_ref": "-1001",
        "kind": "approval",
        "state": "pending",
        "count": 1,
        "oldest_created_at": "2026-09-15T14:58:00Z",
    }
    row.update(over)
    return row


def burst_row(ws: str, section: str, **fields) -> dict:
    row = {
        "workspace_id": ws,
        "section": section,
        "at": "2026-09-15T14:51:00Z",
        "intent_id": INTENT,
    }
    row.update(fields)
    return row


def both(kind: str, rows_a: list, rows_b: list, key: str | None = None) -> dict:
    """Routes for one view in both workspaces."""
    return {
        ("GET", path(kind, WS_A, key)): view(kind, WS_A, rows_a),
        ("GET", path(kind, WS_B, key)): view(kind, WS_B, rows_b),
    }


# --- the loop and the one shape ----------------------------------------------


def test_floating_loops_every_workspace_into_one_envelope(tmp_path):
    api = reads_api(
        both("floating", [floating_row(WS_A)], [floating_row(WS_B, OTHER_INTENT)])
    )
    result = run(read_runtime(tmp_path, api), "--json", "floating")
    assert result.exit_code == EXIT_OK, result.output
    document = one_envelope(result)
    assert document["kind"] == "floating"
    rows = workspaces_of(document)
    assert list(rows) == [WS_A, WS_B]
    assert rows[WS_A] == [floating_row(WS_A)]
    assert rows[WS_B] == [floating_row(WS_B, OTHER_INTENT)]
    assert [p for p, _ in ops_calls(api)] == [
        path("floating", WS_A),
        path("floating", WS_B),
    ]


def test_floating_human_is_a_table_per_workspace(tmp_path):
    api = reads_api(both("floating", [floating_row(WS_A)], []))
    result = run(read_runtime(tmp_path, api), "floating")
    assert result.exit_code == EXIT_OK, result.output
    out = result.stdout
    assert WS_A in out and WS_B in out
    assert INTENT in out
    for word in ("container_created", "ready", "container_not_ready"):
        assert word in out, word
    assert "nothing floating" in out


def test_a_single_workspace_read_has_the_same_shape(tmp_path):
    api = reads_api(both("floating", [floating_row(WS_A)], []))
    result = run(read_runtime(tmp_path, api), "--json", "floating", "--workspace", WS_A)
    assert result.exit_code == EXIT_OK, result.output
    assert workspaces_of(one_envelope(result)) == {WS_A: [floating_row(WS_A)]}


def test_floating_limit_travels_as_a_query(tmp_path):
    api = reads_api(both("floating", [], []))
    result = run(read_runtime(tmp_path, api), "floating", "--limit", "5", "--json")
    assert result.exit_code == EXIT_OK, result.output
    assert ops_calls(api) == [
        (path("floating", WS_A), {"limit": "5"}),
        (path("floating", WS_B), {"limit": "5"}),
    ]


def test_floating_without_a_limit_sends_none(tmp_path):
    api = reads_api(both("floating", [], []))
    run(read_runtime(tmp_path, api), "--json", "floating")
    assert all(params == {} for _, params in ops_calls(api))


@pytest.mark.parametrize("value", ["0", "501"])
def test_a_bad_limit_is_usage(tmp_path, value):
    result = run(read_runtime(tmp_path, reads_api()), "floating", "--limit", value)
    assert result.exit_code == EXIT_USAGE


def test_a_story_id_that_is_not_a_uuid_is_usage(tmp_path):
    api = reads_api()
    result = run(read_runtime(tmp_path, api), "story", "0395b173")
    assert result.exit_code == EXIT_USAGE, result.output
    assert "full UUID" in result.stderr
    assert ops_calls(api) == []


def test_an_interval_below_one_second_is_usage(tmp_path):
    result = run(
        read_runtime(tmp_path, reads_api()), "floating", "--watch", "--every", "0.5"
    )
    assert result.exit_code == EXIT_USAGE


# --- --workspace ----------------------------------------------------------------


def test_workspace_by_id_reads_that_workspace_only(tmp_path):
    api = reads_api(both("floating", [floating_row(WS_A)], [floating_row(WS_B)]))
    result = run(read_runtime(tmp_path, api), "--json", "floating", "--workspace", WS_B)
    assert result.exit_code == EXIT_OK, result.output
    assert list(workspaces_of(one_envelope(result))) == [WS_B]
    assert [p for p, _ in ops_calls(api)] == [path("floating", WS_B)]
    assert "/api/v1/me/principal" not in api.paths(), (
        "an id needs no lookup — the API is the authority on membership"
    )


def test_workspace_by_exact_name_resolves_through_the_principal(tmp_path):
    api = reads_api(both("floating", [floating_row(WS_A)], [floating_row(WS_B)]))
    result = run(
        read_runtime(tmp_path, api), "--json", "floating", "--workspace", NAME_B
    )
    assert result.exit_code == EXIT_OK, result.output
    assert list(workspaces_of(one_envelope(result))) == [WS_B]
    assert api.paths()[0] == "/api/v1/me/principal"
    assert [p for p, _ in ops_calls(api)] == [path("floating", WS_B)]


def test_a_name_shared_by_two_workspaces_reads_both(tmp_path):
    """Two studios called the same thing are both read — a name is a filter
    over the principal's list, not a pick of its first match."""
    twins = {
        **PERSON,
        "workspaces": [
            {"id": WS_A, "name": "Studio", "role": "owner"},
            {"id": WS_B, "name": "Studio", "role": "member"},
        ],
    }
    api = reads_api(
        both("floating", [floating_row(WS_A)], [floating_row(WS_B)]), principal=twins
    )
    result = run(
        read_runtime(tmp_path, api), "--json", "floating", "--workspace", "Studio"
    )
    assert result.exit_code == EXIT_OK, result.output
    assert list(workspaces_of(one_envelope(result))) == [WS_A, WS_B]
    assert [p for p, _ in ops_calls(api)] == [
        path("floating", WS_A),
        path("floating", WS_B),
    ]


def test_a_workspace_name_that_matches_none_exits_1(tmp_path):
    api = reads_api(both("floating", [], []))
    result = run(
        read_runtime(tmp_path, api), "floating", "--workspace", "Nobody's studio"
    )
    assert result.exit_code == EXIT_NOT_FOUND
    assert "no such workspace" in result.stderr
    assert "whoami" in result.stderr
    assert ops_calls(api) == []


def test_a_workspace_name_that_matches_none_is_an_error_envelope_in_json(tmp_path):
    api = reads_api(both("floating", [], []))
    result = run(
        read_runtime(tmp_path, api), "--json", "floating", "--workspace", "nope"
    )
    assert result.exit_code == EXIT_NOT_FOUND
    document = one_envelope(result)
    assert document["kind"] == "floating"
    assert document["error"]["reason"] == "not_found"


def test_a_workspace_name_match_is_exact(tmp_path):
    api = reads_api(both("floating", [], []))
    result = run(read_runtime(tmp_path, api), "floating", "--workspace", NAME_B.lower())
    assert result.exit_code == EXIT_NOT_FOUND


# --- exit codes 3 and 4 through the mapping main owns ----------------------------


def test_a_workspace_the_principal_cannot_see_exits_3(tmp_path):
    api = reads_api(both("floating", [], []))
    api.routes[("GET", path("floating", WS_B))] = (404, {"detail": "not found"})
    result = run(read_runtime(tmp_path, api), "floating")
    assert result.exit_code == EXIT_NOT_AUTHORIZED
    assert REASON_SENTENCES["not_a_member"] in result.stderr


def test_a_workspace_id_the_api_refuses_exits_3_in_json(tmp_path):
    other = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    api = reads_api()  # anything unscripted is the real API's 404
    result = run(
        read_runtime(tmp_path, api), "--json", "floating", "--workspace", other
    )
    assert result.exit_code == EXIT_NOT_AUTHORIZED
    document = one_envelope(result)
    assert document["error"]["code"] == EXIT_NOT_AUTHORIZED
    assert document["error"]["reason"] == "not_a_member"


def test_a_403_with_a_reason_uses_the_reason_sentence(tmp_path):
    api = reads_api(both("jobs", [], []))
    api.routes[("GET", path("jobs", WS_A))] = (
        403,
        {"detail": "wrong workspace", "reason": "wrong_workspace"},
    )
    result = run(read_runtime(tmp_path, api), "jobs")
    assert result.exit_code == EXIT_NOT_AUTHORIZED
    assert REASON_SENTENCES["wrong_workspace"] in result.stderr


def test_a_401_exits_3(tmp_path):
    api = reads_api()
    api.routes[("GET", "/api/v1/me/principal")] = (
        401,
        {"detail": "authentication required"},
    )
    result = run(read_runtime(tmp_path, api), "--json", "outbox")
    assert result.exit_code == EXIT_NOT_AUTHORIZED
    assert one_envelope(result)["error"]["reason"] == "not_authorized"


def test_a_connection_error_exits_4(tmp_path):
    api = reads_api(both("floating", [], []))
    api.routes[("GET", path("floating", WS_A))] = httpx.ConnectError(
        "connection refused"
    )
    result = run(read_runtime(tmp_path, api), "floating")
    assert result.exit_code == EXIT_API_UNREACHABLE
    assert "STORYDUMP_API" in result.stderr


def test_a_connection_error_is_an_error_envelope_in_json(tmp_path):
    api = reads_api()
    api.routes[("GET", "/api/v1/me/principal")] = httpx.ConnectError(
        "connection refused"
    )
    result = run(read_runtime(tmp_path, api), "--json", "burst")
    assert result.exit_code == EXIT_API_UNREACHABLE
    document = one_envelope(result)
    assert document["kind"] == "burst"
    assert document["error"]["reason"] == "api_unreachable"


def test_an_answer_that_is_not_the_views_envelope_exits_4(tmp_path):
    api = reads_api(both("floating", [], []))
    api.routes[("GET", path("floating", WS_A))] = (200, {"rows": []})
    result = run(read_runtime(tmp_path, api), "--json", "floating")
    assert result.exit_code == EXIT_API_UNREACHABLE
    assert one_envelope(result)["error"]["reason"] == "api_unreachable"


def test_without_a_token_no_read_reaches_the_api(tmp_path):
    api = reads_api()
    result = run(read_runtime(tmp_path, api, token=None), "floating")
    assert result.exit_code == EXIT_NOT_AUTHORIZED
    assert api.calls == []


# --- story ----------------------------------------------------------------------


def test_story_json_is_the_one_row_under_its_workspace(tmp_path):
    api = reads_api(both("story", [story_row(WS_A)], [], key=INTENT))
    result = run(read_runtime(tmp_path, api), "--json", "story", INTENT)
    assert result.exit_code == EXIT_OK, result.output
    document = one_envelope(result)
    assert document["kind"] == "story"
    rows = workspaces_of(document)
    assert rows[WS_A][0]["intent"]["id"] == INTENT
    assert rows[WS_B] == []
    assert [p for p, _ in ops_calls(api)] == [
        path("story", WS_A, INTENT),
        path("story", WS_B, INTENT),
    ]


def test_story_human_has_sections(tmp_path):
    api = reads_api(both("story", [story_row(WS_A)], [], key=INTENT))
    result = run(read_runtime(tmp_path, api), "story", INTENT)
    assert result.exit_code == EXIT_OK, result.output
    out = result.stdout
    assert INTENT in out
    for word in ("approved", "container_created", "audit", "operations", "outbox"):
        assert word in out, word
    assert "awaiting_approval" in out and "create_container" in out and "fresh" in out
    assert "approval" in out and "1234" in out


def test_story_found_nowhere_exits_1_with_the_sentence(tmp_path):
    api = reads_api(both("story", [], [], key=INTENT))
    result = run(read_runtime(tmp_path, api), "story", INTENT)
    assert result.exit_code == EXIT_NOT_FOUND
    assert "no such story" in result.stderr
    assert len(ops_calls(api)) == 2, "every workspace is asked before the answer is no"


def test_story_found_nowhere_is_an_error_envelope_in_json(tmp_path):
    api = reads_api(both("story", [], [], key=INTENT))
    result = run(read_runtime(tmp_path, api), "--json", "story", INTENT)
    assert result.exit_code == EXIT_NOT_FOUND
    document = one_envelope(result)
    assert document["kind"] == "story"
    assert document["error"]["reason"] == "not_found"
    assert document["error"]["code"] == EXIT_NOT_FOUND


def test_story_found_in_the_second_workspace_only_is_success(tmp_path):
    api = reads_api(both("story", [], [story_row(WS_B)], key=INTENT))
    result = run(read_runtime(tmp_path, api), "--json", "story", INTENT)
    assert result.exit_code == EXIT_OK, result.output
    rows = workspaces_of(one_envelope(result))
    assert rows[WS_A] == [] and rows[WS_B][0]["intent"]["id"] == INTENT


# --- cards ----------------------------------------------------------------------


def test_cards_json_and_human(tmp_path):
    api = reads_api(both("cards", [cards_row(WS_A)], [], key=INTENT))
    rt = read_runtime(tmp_path, api)
    result = run(rt, "--json", "cards", INTENT)
    assert result.exit_code == EXIT_OK, result.output
    document = one_envelope(result)
    assert document["kind"] == "cards"
    assert workspaces_of(document)[WS_A] == [cards_row(WS_A)]
    result = run(read_runtime(tmp_path, api), "cards", INTENT)
    assert result.exit_code == EXIT_OK, result.output
    assert f"workspace {WS_A}" in result.stdout, "the human frame, not JSON"
    for word in ("binding", "message ref", "telegram", "approval", "sent", "1234"):
        assert word in result.stdout, word
    assert "nothing sent for it here" in result.stdout


def test_cards_found_nowhere_exits_1(tmp_path):
    api = reads_api(both("cards", [], [], key=INTENT))
    result = run(read_runtime(tmp_path, api), "cards", INTENT)
    assert result.exit_code == EXIT_NOT_FOUND
    assert "no such story" in result.stderr


# --- account --------------------------------------------------------------------


def test_account_by_handle_json_and_human(tmp_path):
    api = reads_api(both("account", [account_row(WS_A)], [], key="storydump.studio"))
    rt = read_runtime(tmp_path, api)
    result = run(rt, "--json", "account", "storydump.studio")
    assert result.exit_code == EXIT_OK, result.output
    document = one_envelope(result)
    assert document["kind"] == "account"
    assert workspaces_of(document)[WS_A][0]["handle"] == "storydump.studio"
    assert [p for p, _ in ops_calls(api)][0] == path(
        "account", WS_A, "storydump.studio"
    )
    result = run(read_runtime(tmp_path, api), "account", "storydump.studio")
    assert result.exit_code == EXIT_OK, result.output
    out = result.stdout
    assert f"workspace {WS_A}" in out, "the human frame, not JSON"
    for word in (
        "@storydump.studio",
        "America/New_York",
        "2026-09-15T18:00:00Z",
        "posted",
        INTENT,
    ):
        assert word in out, word
    assert "2/3 on 2026-09-15" in out
    assert "no such account here" in out


def test_account_key_is_one_path_segment(tmp_path):
    api = reads_api()
    result = run(read_runtime(tmp_path, api), "account", "a/b", "--workspace", WS_A)
    assert result.exit_code == EXIT_NOT_AUTHORIZED  # the scripted 404
    assert str(api.calls[-1].url.raw_path, "ascii") == path("account", WS_A, "a%2Fb")


def test_account_found_nowhere_exits_1(tmp_path):
    api = reads_api(both("account", [], [], key="ghost"))
    result = run(read_runtime(tmp_path, api), "--json", "account", "ghost")
    assert result.exit_code == EXIT_NOT_FOUND
    document = one_envelope(result)
    assert document["error"]["reason"] == "not_found"
    assert "no such account" in document["error"]["detail"]


# --- jobs, outbox, burst and --since ----------------------------------------------


def test_jobs_default_since_is_three_hours_before_now(tmp_path):
    api = reads_api(both("jobs", [jobs_row(WS_A)], []))
    result = run(read_runtime(tmp_path, api), "--json", "jobs")
    assert result.exit_code == EXIT_OK, result.output
    assert ops_calls(api) == [
        (path("jobs", WS_A), {"since": "2026-09-15T12:00:00Z"}),
        (path("jobs", WS_B), {"since": "2026-09-15T12:00:00Z"}),
    ]
    assert workspaces_of(one_envelope(result))[WS_A] == [jobs_row(WS_A)]


@pytest.mark.parametrize(
    ("value", "sent"),
    [
        ("15m", "2026-09-15T14:45:00Z"),
        ("2h", "2026-09-15T13:00:00Z"),
        ("1d", "2026-09-14T15:00:00Z"),
        ("2026-09-15T14:50:00Z", "2026-09-15T14:50:00Z"),
        ("2026-09-15T16:50:00+02:00", "2026-09-15T14:50:00Z"),
        ("2026-09-15T14:50:00", "2026-09-15T14:50:00Z"),
        ("2026-09-15", "2026-09-15T00:00:00Z"),
    ],
)
def test_since_forms_all_reach_the_api_as_utc(tmp_path, value, sent):
    api = reads_api(both("burst", [], []))
    result = run(read_runtime(tmp_path, api), "--json", "burst", "--since", value)
    assert result.exit_code == EXIT_OK, result.output
    assert {params["since"] for _, params in ops_calls(api)} == {sent}


@pytest.mark.parametrize(
    "value",
    ["yesterday", "3", "3w", "-3h", "", "2026-13-40", "31d", "999999d", "2027-01-01"],
)
def test_a_bad_since_is_usage_64(tmp_path, value):
    api = reads_api(both("outbox", [], []))
    result = run(read_runtime(tmp_path, api), "outbox", "--since", value)
    assert result.exit_code == EXIT_USAGE, result.output
    assert "--since" in result.stderr
    assert ops_calls(api) == []


def test_a_bad_since_in_json_is_a_usage_envelope(tmp_path):
    result = run(
        read_runtime(tmp_path, reads_api()), "--json", "jobs", "--since", "soon"
    )
    assert result.exit_code == EXIT_USAGE
    document = one_envelope(result)
    assert document["kind"] == "jobs"
    assert document["error"]["reason"] == "usage"


def test_jobs_human_shows_groups_and_the_failed_samples(tmp_path):
    failed = jobs_row(
        WS_A,
        state="failed",
        count=1,
        samples=[
            {
                "id": JOB_ID,
                "attempts": 5,
                "run_at": "2026-09-15T14:30:00Z",
                "error": "container refused",
            }
        ],
    )
    api = reads_api(both("jobs", [jobs_row(WS_A), failed], []))
    result = run(read_runtime(tmp_path, api), "jobs")
    assert result.exit_code == EXIT_OK, result.output
    out = result.stdout
    for word in ("publish", "ready", "failed", JOB_ID, "container refused", "no jobs"):
        assert word in out, word


def test_outbox_json_and_human(tmp_path):
    api = reads_api(both("outbox", [outbox_row(WS_A)], []))
    rt = read_runtime(tmp_path, api)
    result = run(rt, "--json", "outbox", "--since", "1h")
    assert result.exit_code == EXIT_OK, result.output
    document = one_envelope(result)
    assert document["kind"] == "outbox"
    assert workspaces_of(document)[WS_A] == [outbox_row(WS_A)]
    assert ops_calls(api)[0][1] == {"since": "2026-09-15T14:00:00Z"}
    result = run(read_runtime(tmp_path, api), "outbox")
    assert result.exit_code == EXIT_OK, result.output
    assert f"workspace {WS_A}" in result.stdout, "the human frame, not JSON"
    for word in ("binding", "oldest", "telegram", "approval", "pending", BINDING):
        assert word in result.stdout, word
    assert "outbox empty" in result.stdout


def test_burst_json_is_one_flat_timeline_per_workspace(tmp_path):
    rows = [
        burst_row(
            WS_A,
            "tap",
            from_state="awaiting_approval",
            to_state="approved",
            actor_kind="user",
            channel="telegram",
        ),
        burst_row(
            WS_A,
            "permit",
            generation=1,
            state="permitted",
            url_variant="fresh",
            error=None,
            subcode=None,
            elapsed_ms=812,
        ),
        burst_row(
            WS_A,
            "float_wait",
            wait_class="container_not_ready",
            rung=1,
            seconds=30,
            next_run_at="2026-09-15T14:51:30Z",
        ),
        burst_row(
            WS_A,
            "sibling",
            posted_id=OTHER_INTENT,
            waiting_id=INTENT,
            wait_class="container_not_ready",
        ),
        burst_row(WS_A, "review", from_state="publishing", last_error="ambiguous"),
        burst_row(WS_A, "outcome", at=None, intent_id=None, state="posted", count=13),
    ]
    api = reads_api(both("burst", rows, []))
    rt = read_runtime(tmp_path, api)
    result = run(rt, "--json", "burst")
    assert result.exit_code == EXIT_OK, result.output
    document = one_envelope(result)
    assert document["kind"] == "burst"
    assert workspaces_of(document)[WS_A] == rows
    result = run(read_runtime(tmp_path, api), "burst")
    assert result.exit_code == EXIT_OK, result.output
    out = result.stdout
    assert f"workspace {WS_A}" in out, "the human frame, not JSON"
    for section in ("tap", "permit", "float_wait", "sibling", "review", "outcome"):
        assert section in out, section
    for word in (
        "approved",
        "permitted",
        "fresh",
        "container_not_ready",
        OTHER_INTENT,
        "ambiguous",
        "posted",
        "13",
    ):
        assert word in out, word
    assert "nothing in the window" in out


# --- posture --------------------------------------------------------------------

POSTURE = {
    "migrations": [
        {
            "version": "077",
            "checksum": "abc123",
            "applied_at": "2026-09-15T23:05:00Z",
            "status": "applied",
        }
    ],
    "role": {"user": "svc_ingress", "bypassrls": False},
    "rls": [{"table": "post_intents", "enabled": True, "forced": True}],
    "doors": [{"name": "admit_webhook", "owner": "svc_owner"}],
}


def test_posture_json_is_the_views_object(tmp_path):
    api = reads_api()
    api.routes[("GET", "/api/v1/ops/posture")] = (
        200,
        {"v": 1, "kind": "posture", "data": POSTURE, "error": None},
    )
    result = run(read_runtime(tmp_path, api), "--json", "posture")
    assert result.exit_code == EXIT_OK, result.output
    document = one_envelope(result)
    assert document["kind"] == "posture"
    assert document["data"] == POSTURE
    assert api.paths() == ["/api/v1/ops/posture"], "posture is not a workspace read"


def test_posture_human(tmp_path):
    api = reads_api()
    api.routes[("GET", "/api/v1/ops/posture")] = (
        200,
        {"v": 1, "kind": "posture", "data": POSTURE, "error": None},
    )
    result = run(read_runtime(tmp_path, api), "posture")
    assert result.exit_code == EXIT_OK, result.output
    out = result.stdout
    for word in (
        "077",
        "applied",
        "svc_ingress",
        "bypassrls",
        "post_intents",
        "forced",
        "admit_webhook",
        "svc_owner",
    ):
        assert word in out, word


def test_posture_has_no_workspace_and_no_watch(tmp_path):
    rt = read_runtime(tmp_path, reads_api())
    assert run(rt, "posture", "--workspace", WS_A).exit_code == EXIT_USAGE
    assert run(rt, "posture", "--watch").exit_code == EXIT_USAGE


# --- redaction --------------------------------------------------------------------


def test_a_token_inside_a_row_is_redacted_in_both_modes(tmp_path):
    leaked = story_row(WS_A, last_error=f"refused {SECRET} by postgres://u:p@h/db")
    api = reads_api(both("story", [leaked], [], key=INTENT))
    rt = read_runtime(tmp_path, api)
    result = run(rt, "story", INTENT)
    assert result.exit_code == EXIT_OK, result.output
    assert SECRET not in result.output and "postgres://u:" not in result.output
    assert "sdt_…" in result.stdout
    result = run(read_runtime(tmp_path, api), "--json", "story", INTENT)
    assert SECRET not in result.output
    document = one_envelope(result)
    assert document["data"]["workspaces"][0]["rows"][0]["intent"]["last_error"] == (
        "refused sdt_… by postgres://<redacted>"
    )


def test_a_token_inside_a_floating_row_is_redacted_in_human_mode(tmp_path):
    api = reads_api(both("floating", [floating_row(WS_A, last_wait_class=SECRET)], []))
    result = run(read_runtime(tmp_path, api), "floating")
    assert result.exit_code == EXIT_OK, result.output
    assert SECRET not in result.output


# --- help ---------------------------------------------------------------------------


@pytest.mark.parametrize("verb", [*VIEWS, "posture"])
def test_every_read_verb_has_help_with_an_example(tmp_path, verb):
    result = run(read_runtime(tmp_path, reads_api()), verb, "--help")
    assert result.exit_code == EXIT_OK, result.output
    assert f"storydump {verb}" in result.stdout
    assert "Example" in result.stdout


@pytest.mark.parametrize("verb", VIEWS)
def test_every_view_verb_offers_workspace_watch_and_json(tmp_path, verb):
    result = run(read_runtime(tmp_path, reads_api()), verb, "--help")
    for flag in ("--workspace", "--watch", "--every", "--json"):
        assert flag in result.stdout, flag


def test_every_without_watch_is_usage(tmp_path):
    result = run(read_runtime(tmp_path, reads_api()), "floating", "--every", "5")
    assert result.exit_code == EXIT_USAGE
    assert "--watch" in result.stderr


def test_the_root_help_lists_the_read_verbs(tmp_path):
    result = run(read_runtime(tmp_path, reads_api()), "--help")
    for verb in (*VIEWS, "posture"):
        assert verb in result.stdout, verb
    assert "6" in result.stdout


def test_every_json_document_is_an_envelope(tmp_path):
    """The one rule the modes share: whatever a read prints in JSON mode is
    one well-formed envelope per line."""
    api = reads_api(
        {
            **both("story", [story_row(WS_A)], [], key=INTENT),
            **both("floating", [floating_row(WS_A)], []),
            **both("jobs", [jobs_row(WS_A)], []),
        }
    )
    rt = read_runtime(tmp_path, api)
    for args in (
        ("story", INTENT),
        ("floating",),
        ("jobs",),
        ("floating", "--workspace", "none"),
    ):
        result = run(rt, "--json", *args)
        for line in result.stdout.splitlines():
            check_envelope(json.loads(line))


def test_a_story_at_the_views_bound_says_its_older_rows_are_omitted(tmp_path):
    """The API keeps a list's NEWEST rows at the bound and names the list; the
    terminal says so, so an operator never reads a timeline that quietly ends."""
    row = story_row(WS_A)
    row["truncated"] = ["audit"]
    api = reads_api({("GET", path("story", WS_A, INTENT)): view("story", WS_A, [row])})
    result = run(read_runtime(tmp_path, api), "story", INTENT, "--workspace", WS_A)
    assert result.exit_code == EXIT_OK, result.output
    assert "older ones are omitted" in result.stdout and "audit" in result.stdout
    row["truncated"] = []
    api = reads_api({("GET", path("story", WS_A, INTENT)): view("story", WS_A, [row])})
    result = run(read_runtime(tmp_path, api), "story", INTENT, "--workspace", WS_A)
    assert "omitted" not in result.stdout
