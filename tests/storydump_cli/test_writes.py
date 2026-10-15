"""The write verbs: ``approve``, ``skip``, ``reject``, ``posted``, ``cancel``,
``resolve``, ``pause``, ``resume``, ``sync`` — each one POST to the command
port with the bearer token and a DETERMINISTIC idempotency key, so a re-run
replays ("already done", exit 0) rather than executing twice.

What is pinned: the route, the key and the body per verb; a refusal is an
answer (the CLI's sentence for the reason, the fixing verb, exit 2); a
readonly token or a service identity exits 3; ``--idempotency-key`` is the
deliberate second execution; ``--workspace`` is required and a name resolves
through the principal; the envelope's ``data`` shape; redaction inside the
port's answer; no Telegram word in any sentence.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import click
import httpx
import pytest

from src.services.target.vocabulary import (
    EXIT_API_UNREACHABLE,
    EXIT_NOT_AUTHORIZED,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_USAGE,
    IDEMPOTENCY_HEADER,
    OUTCOME_SENTENCES,
    REASON_SENTENCES,
    TAP_WORDS,
    WRITE_SENTENCES,
)
from storydump_cli.commands import FIXES, INTERRUPTED_FIX, writes
from tests.storydump_cli.test_main import (
    PERSON,
    WS,
    Api,
    one_envelope,
    run,
    runtime,
)

INTENT = "0395b173-9c3e-4a6b-8f2e-6d1c2b3a4f50"
SOURCE = "5a5a5a5a-5a5a-4a5a-8a5a-5a5a5a5a5a5a"
WS_B = "55555555-5555-4555-8555-555555555555"
NOW = datetime(2026, 9, 15, 15, 0, tzinfo=timezone.utc)
UUID = "9d9d9d9d-9d9d-4d9d-8d9d-9d9d9d9d9d9d"
EPISODE = "2026-09-15T14:00:00+00:00"

TWO = {
    **PERSON,
    "workspaces": [
        {"id": WS, "name": "Example studio", "role": "owner"},
        {"id": WS_B, "name": "Second studio", "role": "member"},
    ],
}

#: verb → (the port's command, the story argument it takes)
STORY_VERBS = {
    "approve": "approve",
    "skip": "skip",
    "reject": "reject",
    "posted": "mark_posted",
    "cancel": "cancel",
}


def route(ws: str, command: str) -> tuple[str, str]:
    return ("POST", f"/api/v1/workspaces/{ws}/commands/{command}")


def write_api(routes=None, principal=TWO) -> Api:
    api = Api({("GET", "/api/v1/me/principal"): (200, principal)})
    api.routes.update(routes or {})
    return api


def write_runtime(tmp_path, api: Api, **kw):
    rt = runtime(tmp_path, api, **kw)
    rt.now_fn = lambda: NOW
    rt.uuid_fn = lambda: UUID
    return rt


def story_route(ws: str, intent_id: str, episode: str = EPISODE) -> dict:
    """The story view a resolution reads its episode from."""
    return {
        ("GET", f"/api/v1/ops/workspaces/{ws}/story/{intent_id}"): (
            200,
            {
                "v": 1,
                "kind": "story",
                "data": {
                    "workspace_id": ws,
                    "rows": [
                        {
                            "workspace_id": ws,
                            "intent": {
                                "id": intent_id,
                                "state": "review_required",
                                "entered_state_at": episode,
                            },
                            "audit": [],
                            "operations": [],
                            "cards": [],
                        }
                    ],
                },
                "error": None,
            },
        )
    }


def posts(api: Api) -> list[httpx.Request]:
    return [r for r in api.calls if r.method == "POST"]


def body_of(request: httpx.Request) -> dict:
    return json.loads(request.content or b"{}")


# --- the route, the key, the body ---------------------------------------------


@pytest.mark.parametrize("verb,command", sorted(STORY_VERBS.items()))
def test_each_story_verb_posts_its_command_with_the_deterministic_key(
    tmp_path, verb, command
):
    api = write_api({route(WS, command): (200, {"outcome": "executed", "state": "x"})})
    result = run(write_runtime(tmp_path, api), verb, INTENT, "--workspace", WS)
    assert result.exit_code == EXIT_OK, result.output
    (request,) = posts(api)
    assert request.url.path == f"/api/v1/workspaces/{WS}/commands/{command}"
    assert request.headers["Idempotency-Key"] == f"{command}:{INTENT}", (
        "the web's convention — a re-run replays"
    )
    assert request.headers["Authorization"].startswith("Bearer sdt_")
    assert body_of(request) == {"intent_id": INTENT}


def test_the_keys_match_the_shared_fixture_both_doors_read():
    """`tests/fixtures/idempotency_keys.json` is read by this test and by the
    web's `idempotency-fixture.test.ts`: one statement of each door's rule,
    the deliberate differences included."""
    import json
    from pathlib import Path

    fixture = json.loads(
        (
            Path(__file__).resolve().parents[1] / "fixtures" / "idempotency_keys.json"
        ).read_text()
    )
    for case in fixture["cases"]:
        body, command = case["body"], case["command"]
        if "submission_id" in body:
            key = writes.fresh_key(
                command,
                workspace_id=fixture["workspace"],
                identity=body["submission_id"],
            )
        else:
            key = writes.deterministic_key(
                command,
                intent_id=body["intent_id"],
                resolution=body.get("resolution"),
                verdict=body.get("verdict"),
                episode=body.get("episode"),
                stop_repeat=body.get("stop_repeat", False),
            )
        assert key == case["cli"], (command, body)


def test_the_deterministic_key_is_a_function_of_command_and_story():
    assert writes.deterministic_key("skip", intent_id=INTENT) == f"skip:{INTENT}"
    assert (
        writes.deterministic_key(
            "resolve_review", intent_id=INTENT, resolution="retry", episode=EPISODE
        )
        == f"resolve_review:{INTENT}:retry:{EPISODE}"
    )
    assert (
        writes.deterministic_key(
            "resolve_review",
            intent_id=INTENT,
            resolution="retry",
            verdict="not_posted",
            episode=EPISODE,
        )
        == f"resolve_review:{INTENT}:retry:not_posted:{EPISODE}"
    )
    assert (
        writes.deterministic_key(
            "resolve_review", intent_id=INTENT, resolution="cancel", episode=""
        )
        == f"resolve_review:{INTENT}:cancel"
    ), "no episode: the segment is omitted, as the web omits it"
    assert (
        writes.fresh_key("pause_workspace", workspace_id=WS, identity=UUID)
        == f"pause_workspace:{WS}:{UUID}"
    )


@pytest.mark.parametrize(
    "verb,command", [("pause", "pause_workspace"), ("resume", "resume_workspace")]
)
def test_pause_and_resume_mint_a_fresh_key_per_invocation(tmp_path, verb, command):
    """The web mints a submission id per click; so does the CLI, because a
    day or minute bucket answered "already done" to a second pause after a
    resume while the workspace stayed resumed."""
    api = write_api({route(WS, command): (200, {"outcome": "executed"})})
    result = run(write_runtime(tmp_path, api), verb, "--workspace", WS)
    assert result.exit_code == EXIT_OK, result.output
    (request,) = posts(api)
    assert request.url.path.endswith(f"/commands/{command}")
    assert request.headers["Idempotency-Key"] == f"{command}:{WS}:{UUID}"
    assert body_of(request) == {}


def test_a_second_pause_after_a_resume_is_a_new_key(tmp_path):
    api = write_api(
        {
            route(WS, "pause_workspace"): (200, {"outcome": "executed"}),
            route(WS, "resume_workspace"): (200, {"outcome": "executed"}),
        }
    )
    rt = write_runtime(tmp_path, api)
    minted = iter(["1111", "2222", "3333"])
    rt.uuid_fn = lambda: next(minted)
    for verb in ("pause", "resume", "pause"):
        assert run(rt, verb, "--workspace", WS).exit_code == EXIT_OK
    keys = [r.headers["Idempotency-Key"] for r in posts(api)]
    assert len(set(keys)) == 3, keys


def test_sync_names_the_source_and_mints_a_fresh_key(tmp_path):
    api = write_api({route(WS, "sync_now"): (202, {"outcome": "enqueued"})})
    result = run(write_runtime(tmp_path, api), "sync", SOURCE, "--workspace", WS)
    assert result.exit_code == EXIT_OK, result.output
    (request,) = posts(api)
    assert body_of(request) == {"source_id": SOURCE}
    assert request.headers["Idempotency-Key"] == f"sync_now:{WS}:{UUID}"
    assert WRITE_SENTENCES[("sync_now", "enqueued")] in result.stdout


def test_sync_of_an_unknown_source_says_source_not_story(tmp_path):
    api = write_api(
        {
            route(WS, "sync_now"): (
                404,  # the port's `not_found` is a 404 (`app._COMMAND_STATUS`)
                {"reason": "not_found", "detail": "media source x"},
            )
        }
    )
    result = run(write_runtime(tmp_path, api), "sync", SOURCE, "--workspace", WS)
    assert result.exit_code == EXIT_NOT_FOUND, result.output
    assert "media source" in result.stderr
    assert "story" not in result.stderr.split("fix:")[0]
    assert "Settings › Integrations" in result.stderr


def test_a_settled_story_answers_with_its_state(tmp_path):
    """A command on a story past awaiting_approval ANSWERS with the state
    (the port's fourth outcome): the CLI says nothing changed, and which state."""
    api = write_api(
        {route(WS, "approve"): (200, {"outcome": "answered", "state": "skipped"})}
    )
    result = run(write_runtime(tmp_path, api), "approve", INTENT, "--workspace", WS)
    assert result.exit_code == EXIT_OK, result.output
    assert OUTCOME_SENTENCES["answered"] in result.stdout
    assert "(now skipped)" in result.stdout


def test_a_source_id_that_is_not_a_uuid_is_usage(tmp_path):
    api = write_api()
    result = run(write_runtime(tmp_path, api), "sync", "events", "--workspace", WS)
    assert result.exit_code == EXIT_USAGE
    assert posts(api) == []


def test_resolve_carries_the_resolution_and_the_verdict_only_when_asked(tmp_path):
    api = write_api(
        {
            route(WS, "resolve_review"): (202, {"outcome": "enqueued"}),
            **story_route(WS, INTENT),
        }
    )
    rt = write_runtime(tmp_path, api)
    assert run(rt, "resolve", INTENT, "retry", "--workspace", WS).exit_code == EXIT_OK
    assert body_of(posts(api)[0]) == {"intent_id": INTENT, "resolution": "retry"}
    # the web's identity: the story, the resolution and the review EPISODE
    # (`entered_state_at`), read from the story view first
    assert posts(api)[0].headers["Idempotency-Key"] == (
        f"resolve_review:{INTENT}:retry:{EPISODE}"
    )
    assert f"/api/v1/ops/workspaces/{WS}/story/{INTENT}" in api.paths()

    result = run(rt, "resolve", INTENT, "retry", "--not-posted", "--workspace", WS)
    assert result.exit_code == EXIT_OK, result.output
    assert body_of(posts(api)[1]) == {
        "intent_id": INTENT,
        "resolution": "retry",
        "verdict": "not_posted",
    }, "the member's verdict for a lost publish answer rides the command"
    assert posts(api)[1].headers["Idempotency-Key"] == (
        f"resolve_review:{INTENT}:retry:not_posted:{EPISODE}"
    ), (
        "the verdict is part of the identity — a refused retry then --not-posted is two keys"
    )


def test_a_later_review_of_the_same_story_is_a_new_key(tmp_path):
    api = write_api(
        {
            route(WS, "resolve_review"): (202, {"outcome": "enqueued"}),
            **story_route(WS, INTENT),
        }
    )
    rt = write_runtime(tmp_path, api)
    assert run(rt, "resolve", INTENT, "retry", "--workspace", WS).exit_code == EXIT_OK
    api.routes.update(story_route(WS, INTENT, episode="2026-09-16T09:00:00+00:00"))
    assert run(rt, "resolve", INTENT, "retry", "--workspace", WS).exit_code == EXIT_OK
    keys = [r.headers["Idempotency-Key"] for r in posts(api)]
    assert keys[0] != keys[1] and keys[1].endswith("2026-09-16T09:00:00+00:00")


def test_resolve_of_an_unknown_story_is_exit_1_before_any_write(tmp_path):
    """The story view answers an unknown story with no rows (a 404 is a
    workspace the principal cannot see); the CLI answers before any write."""
    empty = {
        ("GET", f"/api/v1/ops/workspaces/{WS}/story/{INTENT}"): (
            200,
            {
                "v": 1,
                "kind": "story",
                "data": {"workspace_id": WS, "rows": []},
                "error": None,
            },
        )
    }
    api = write_api(
        {route(WS, "resolve_review"): (202, {"outcome": "enqueued"}), **empty}
    )
    result = run(
        write_runtime(tmp_path, api), "resolve", INTENT, "retry", "--workspace", WS
    )
    assert result.exit_code == EXIT_NOT_FOUND, result.output
    assert posts(api) == []


def test_a_conflicting_key_names_the_override(tmp_path):
    """The ingress refuses a different command under a key it already holds
    (`admission_conflict`); the CLI names the way out."""
    api = write_api(
        {route(WS, "skip"): (409, {"reason": "admission_conflict", "detail": "x"})}
    )
    result = run(write_runtime(tmp_path, api), "skip", INTENT, "--workspace", WS)
    assert result.exit_code == EXIT_REFUSED, result.output
    assert REASON_SENTENCES["admission_conflict"] in result.stderr
    assert "--idempotency-key" in result.stderr


@pytest.mark.parametrize("resolution", ["posted", "cancel"])
def test_resolve_takes_the_ports_three_resolutions(tmp_path, resolution):
    api = write_api(
        {
            route(WS, "resolve_review"): (200, {"outcome": "executed"}),
            **story_route(WS, INTENT),
        }
    )
    result = run(
        write_runtime(tmp_path, api), "resolve", INTENT, resolution, "--workspace", WS
    )
    assert result.exit_code == EXIT_OK, result.output
    assert body_of(posts(api)[0])["resolution"] == resolution


def test_an_unknown_resolution_is_usage_before_any_call(tmp_path):
    api = write_api()
    result = run(
        write_runtime(tmp_path, api), "resolve", INTENT, "later", "--workspace", WS
    )
    assert result.exit_code == EXIT_USAGE
    assert posts(api) == []


def test_a_story_id_that_is_not_a_uuid_is_usage_before_any_call(tmp_path):
    api = write_api()
    result = run(write_runtime(tmp_path, api), "skip", "0395b173", "--workspace", WS)
    assert result.exit_code == EXIT_USAGE, result.output
    assert posts(api) == []


# --- outcomes -----------------------------------------------------------------


def test_an_enqueued_answer_is_success_with_the_verbs_sentence(tmp_path):
    api = write_api(
        {route(WS, "approve"): (202, {"outcome": "enqueued", "job_id": "j"})}
    )
    result = run(write_runtime(tmp_path, api), "approve", INTENT, "--workspace", WS)
    assert result.exit_code == EXIT_OK, result.output
    assert WRITE_SENTENCES[("approve", "enqueued")] in result.stdout
    assert INTENT in result.stdout


def test_a_replay_is_already_done_and_exit_0(tmp_path):
    api = write_api({route(WS, "skip"): (200, {"outcome": "replayed"})})
    result = run(write_runtime(tmp_path, api), "skip", INTENT, "--workspace", WS)
    assert result.exit_code == EXIT_OK, result.output
    assert OUTCOME_SENTENCES["replayed"] in result.stdout


def test_json_is_one_envelope_with_the_write_shape(tmp_path):
    api = write_api(
        {route(WS, "skip"): (200, {"outcome": "executed", "state": "skipped"})}
    )
    result = run(
        write_runtime(tmp_path, api), "--json", "skip", INTENT, "--workspace", WS
    )
    assert result.exit_code == EXIT_OK, result.output
    document = one_envelope(result)
    assert document["kind"] == "skip"
    assert document["data"] == {
        "workspace_id": WS,
        "command": "skip",
        "args": {"intent_id": INTENT},
        "idempotency_key": f"skip:{INTENT}",
        "outcome": "executed",
        "result": {"state": "skipped"},
    }


def test_the_ports_answer_is_redacted_before_printing(tmp_path):
    leaked = "sdt_" + "z" * 43
    api = write_api({route(WS, "skip"): (200, {"outcome": "executed", "note": leaked})})
    result = run(
        write_runtime(tmp_path, api), "--json", "skip", INTENT, "--workspace", WS
    )
    assert leaked not in result.stdout
    assert one_envelope(result)["data"]["result"]["note"] != leaked


# --- refusals are answers -----------------------------------------------------


def test_a_refusal_prints_the_reason_sentence_and_the_fixing_verb_exit_2(tmp_path):
    api = write_api(
        {
            route(WS, "approve"): (
                409,
                {"reason": "manual_mode", "detail": "the adapter's own words"},
            )
        }
    )
    result = run(write_runtime(tmp_path, api), "approve", INTENT, "--workspace", WS)
    assert result.exit_code == EXIT_REFUSED, result.output
    assert REASON_SENTENCES["manual_mode"] in result.stderr
    assert "the adapter's own words" not in result.stderr
    assert "fix:" in result.stderr


def test_a_not_connected_refusal_points_at_the_accounts_tab(tmp_path):
    """The Instagram connect control lives under Settings › Accounts
    (`landing/src/components/dashboard/settings/accounts-tab.tsx`, "Connect
    Instagram"); the Integrations tab holds the Drive folders and the Telegram
    link."""
    api = write_api(
        {
            route(WS, "approve"): (
                409,
                {"reason": "not_connected", "detail": "the adapter's own words"},
            )
        }
    )
    result = run(write_runtime(tmp_path, api), "approve", INTENT, "--workspace", WS)
    assert result.exit_code == EXIT_REFUSED, result.output
    fix = result.stderr.split("fix:", 1)[1]
    assert "Settings › Accounts" in fix and "Integrations" not in fix


def test_a_lost_publish_answer_names_the_resolve_verb(tmp_path):
    api = write_api(
        {
            route(WS, "resolve_review"): (
                409,
                {"reason": "may_have_posted", "detail": "x"},
            ),
            **story_route(WS, INTENT),
        }
    )
    result = run(
        write_runtime(tmp_path, api), "resolve", INTENT, "retry", "--workspace", WS
    )
    assert result.exit_code == EXIT_REFUSED
    # the reason's sentence names the resolution; the FIX names the verb as typed
    assert "storydump resolve" in result.stderr and "--not-posted" in result.stderr


def test_a_refusal_in_json_is_an_error_envelope_under_the_verb(tmp_path):
    api = write_api(
        {route(WS, "skip"): (409, {"reason": "illegal_transition", "detail": "x"})}
    )
    result = run(
        write_runtime(tmp_path, api), "--json", "skip", INTENT, "--workspace", WS
    )
    assert result.exit_code == EXIT_REFUSED
    document = one_envelope(result)
    assert document["kind"] == "skip" and document["data"] is None
    assert document["error"]["code"] == EXIT_REFUSED
    assert document["error"]["reason"] == "illegal_transition"


@pytest.mark.parametrize("reason", ["readonly_token", "wrong_workspace"])
def test_a_token_that_may_not_write_exits_3(tmp_path, reason):
    api = write_api({route(WS, "skip"): (403, {"reason": reason, "detail": "no"})})
    result = run(write_runtime(tmp_path, api), "skip", INTENT, "--workspace", WS)
    assert result.exit_code == EXIT_NOT_AUTHORIZED, result.output
    assert REASON_SENTENCES[reason] in result.stderr


def test_a_reason_less_403_is_the_role_floor_not_a_bad_token(tmp_path):
    """A member below a verb's floor gets the API's bare `forbidden` (no
    reason); the CLI must not send them to `storydump login` — the token is
    fine, the role is not."""
    api = write_api({route(WS, "pause_workspace"): (403, {"detail": "forbidden"})})
    result = run(write_runtime(tmp_path, api), "--json", "pause", "--workspace", WS)
    assert result.exit_code == EXIT_NOT_AUTHORIZED, result.output
    document = one_envelope(result)
    assert document["error"]["reason"] == "insufficient_role"
    assert "role" in document["error"]["detail"]
    assert "login" not in document["error"]["detail"]
    assert "login" not in document["error"]["fix"]


def test_a_port_answer_without_an_outcome_is_exit_4(tmp_path):
    """A 200 that is not the port's shape (an edge, a proxy) is no answer —
    never reported as executed."""
    api = write_api({route(WS, "skip"): (200, {"hello": "world"})})
    result = run(
        write_runtime(tmp_path, api), "--json", "skip", INTENT, "--workspace", WS
    )
    assert result.exit_code == EXIT_API_UNREACHABLE, result.output
    assert one_envelope(result)["error"]["reason"] == "api_unreachable"


@pytest.mark.parametrize("key", ["caf\u00e9", "a\nb", "a\rb", "x\u00a0y"])
def test_an_idempotency_key_that_is_not_printable_ascii_is_usage(tmp_path, key):
    """The header is ASCII by construction; a key that is not is a usage
    error with the sentence, never a traceback from the transport."""
    api = write_api({route(WS, "skip"): (200, {"outcome": "executed"})})
    result = run(
        write_runtime(tmp_path, api),
        "--json",
        "skip",
        INTENT,
        "--workspace",
        WS,
        "--idempotency-key",
        key,
    )
    assert result.exit_code == EXIT_USAGE, result.output
    assert "idempotency-key" in one_envelope(result)["error"]["detail"]
    assert posts(api) == []


def test_a_workspace_the_token_cannot_see_is_exit_3(tmp_path):
    api = write_api()  # the route is unscripted → the real API's 404
    result = run(write_runtime(tmp_path, api), "skip", INTENT, "--workspace", WS_B)
    assert result.exit_code == EXIT_NOT_AUTHORIZED


def test_a_write_needs_a_token_and_makes_no_call_without_one(tmp_path):
    api = write_api({route(WS, "skip"): (200, {"outcome": "executed"})})
    result = run(
        write_runtime(tmp_path, api, token=None), "skip", INTENT, "--workspace", WS
    )
    assert result.exit_code == EXIT_NOT_AUTHORIZED
    assert api.calls == []


def test_an_api_that_does_not_answer_is_exit_4(tmp_path):
    api = write_api({route(WS, "skip"): httpx.ConnectError("refused")})
    result = run(write_runtime(tmp_path, api), "skip", INTENT, "--workspace", WS)
    assert result.exit_code == EXIT_API_UNREACHABLE


# --- the idempotency key ------------------------------------------------------


def test_idempotency_key_option_is_the_deliberate_second_execution(tmp_path):
    api = write_api({route(WS, "skip"): (200, {"outcome": "executed"})})
    result = run(
        write_runtime(tmp_path, api),
        "skip",
        INTENT,
        "--workspace",
        WS,
        "--idempotency-key",
        "second-look",
    )
    assert result.exit_code == EXIT_OK, result.output
    assert posts(api)[0].headers["Idempotency-Key"] == "second-look"


def test_an_idempotency_key_over_the_ports_limit_is_usage(tmp_path):
    api = write_api()
    result = run(
        write_runtime(tmp_path, api),
        "skip",
        INTENT,
        "--workspace",
        WS,
        "--idempotency-key",
        "k" * 201,
    )
    assert result.exit_code == EXIT_USAGE
    assert posts(api) == []


# --- --workspace --------------------------------------------------------------


def test_workspace_is_required_for_a_write(tmp_path):
    api = write_api()
    result = run(write_runtime(tmp_path, api), "skip", INTENT)
    assert result.exit_code == EXIT_USAGE
    assert api.calls == []


def test_a_workspace_name_resolves_through_the_principal(tmp_path):
    api = write_api({route(WS_B, "skip"): (200, {"outcome": "executed"})})
    result = run(
        write_runtime(tmp_path, api), "skip", INTENT, "--workspace", "Second studio"
    )
    assert result.exit_code == EXIT_OK, result.output
    assert api.paths()[0] == "/api/v1/me/principal"
    assert posts(api)[0].url.path == f"/api/v1/workspaces/{WS_B}/commands/skip"


def test_a_workspace_id_needs_no_lookup(tmp_path):
    api = write_api({route(WS, "skip"): (200, {"outcome": "executed"})})
    run(write_runtime(tmp_path, api), "skip", INTENT, "--workspace", WS)
    assert "/api/v1/me/principal" not in api.paths()


def test_a_workspace_name_that_matches_none_is_exit_1(tmp_path):
    api = write_api()
    result = run(write_runtime(tmp_path, api), "skip", INTENT, "--workspace", "nope")
    assert result.exit_code == EXIT_NOT_FOUND
    assert posts(api) == []


def test_a_name_shared_by_two_workspaces_needs_the_id(tmp_path):
    twins = {
        **PERSON,
        "workspaces": [
            {"id": WS, "name": "Studio", "role": "owner"},
            {"id": WS_B, "name": "Studio", "role": "member"},
        ],
    }
    api = write_api(principal=twins)
    result = run(write_runtime(tmp_path, api), "skip", INTENT, "--workspace", "Studio")
    assert result.exit_code == EXIT_USAGE, (
        "a write goes to ONE workspace; a name that names two is ambiguous"
    )
    assert posts(api) == []
    assert "id" in result.stderr


# --- the sentences ------------------------------------------------------------


def test_every_write_sentence_is_the_terminals_not_the_adapters():
    for key, sentence in WRITE_SENTENCES.items():
        assert key[0] in writes.COMMAND_OF.values(), key
        assert key[1] in OUTCOME_SENTENCES, key
        for word in TAP_WORDS:
            assert word not in sentence.lower().split(), (key, sentence)


def test_every_verb_maps_to_a_port_command():
    assert set(writes.COMMAND_OF) == {
        "approve",
        "skip",
        "reject",
        "posted",
        "cancel",
        "resolve",
        "pause",
        "resume",
        "sync",
        "schedule",
        "reschedule",
        "link",
    }
    assert writes.COMMAND_OF["posted"] == "mark_posted"
    assert writes.COMMAND_OF["resolve"] == "resolve_review"
    assert writes.COMMAND_OF["sync"] == "sync_now"
    assert writes.COMMAND_OF["link"] == "set_item_link"


# --- admit (#1723) ----------------------------------------------------------------
#
# The one write outside the command port: an admission belongs to no
# workspace, so `admit` is one POST to `/admissions`, the address in the body.

ADDRESS = "person@example.com"
ADMISSIONS = "/api/v1/admissions"


def admit_api(status=200, body=None) -> Api:
    answer = {"outcome": "admitted"} if body is None else body
    return write_api({("POST", ADMISSIONS): (status, answer)})


def test_admit_posts_the_address_in_the_body_and_says_the_email_is_queued(tmp_path):
    api = admit_api()
    result = run(write_runtime(tmp_path, api), "admit", ADDRESS)
    assert result.exit_code == EXIT_OK, result.output
    assert result.stdout.strip() == "admitted — the email is queued"
    assert api.paths() == [ADMISSIONS], "no workspace is read: an admission has none"
    (request,) = posts(api)
    assert body_of(request) == {"email": ADDRESS, "resend": False}
    assert request.url.query == b"" and ADDRESS not in str(request.url)
    assert request.headers["Authorization"].startswith("Bearer sdt_")


def test_resend_sends_resend_true_and_says_an_email_is_queued(tmp_path):
    api = admit_api(body={"outcome": "resent"})
    result = run(write_runtime(tmp_path, api), "admit", ADDRESS, "--resend")
    assert result.exit_code == EXIT_OK, result.output
    assert body_of(posts(api)[0]) == {"email": ADDRESS, "resend": True}
    assert result.stdout.strip() == "an email is queued"


def test_a_rerun_is_already_admitted_and_exit_0(tmp_path):
    """`admitted_before` is what a re-run is answered: already done, as a
    replay is, and never a failure."""
    api = admit_api(body={"outcome": "admitted_before"})
    result = run(write_runtime(tmp_path, api), "admit", ADDRESS)
    assert result.exit_code == EXIT_OK, result.output
    assert result.stdout.strip() == (
        "already admitted — run it with --resend to send the email again"
    )
    result = run(write_runtime(tmp_path, api), "--json", "admit", ADDRESS)
    assert result.exit_code == EXIT_OK, result.output
    assert one_envelope(result)["data"]["outcome"] == "admitted_before"


def test_admit_json_is_one_envelope_with_the_admission(tmp_path):
    result = run(write_runtime(tmp_path, admit_api()), "--json", "admit", ADDRESS)
    assert result.exit_code == EXIT_OK, result.output
    document = one_envelope(result)
    assert document["kind"] == "admit"
    assert document["data"] == {
        "email": ADDRESS,
        "resend": False,
        "outcome": "admitted",
    }


@pytest.mark.parametrize("status, reason", [(409, "signed_up"), (400, "invalid_email")])
def test_a_refusal_is_its_sentence_and_its_fix_exit_2(tmp_path, status, reason):
    api = admit_api(status, {"detail": "the API's own words", "reason": reason})
    result = run(write_runtime(tmp_path, api), "admit", ADDRESS)
    assert result.exit_code == EXIT_REFUSED, result.output
    assert REASON_SENTENCES[reason] in result.stderr
    assert FIXES[reason] in result.stderr
    assert "the API's own words" not in result.stderr
    result = run(write_runtime(tmp_path, api), "--json", "admit", ADDRESS)
    error = one_envelope(result)["error"]
    assert (error["code"], error["reason"]) == (EXIT_REFUSED, reason)


def test_admit_takes_any_address_and_leaves_what_one_is_to_the_api(tmp_path):
    """Any address: the CLI sends what it was given, and the API's
    `invalid_email` is the answer to one the table cannot hold."""
    api = admit_api(400, {"detail": "x", "reason": "invalid_email"})
    result = run(write_runtime(tmp_path, api), "admit", "not an address")
    assert result.exit_code == EXIT_REFUSED, result.output
    assert body_of(posts(api)[0])["email"] == "not an address"


@pytest.mark.parametrize("reason", ["not_ops", "readonly_token"])
def test_a_token_refusal_is_exit_3_with_its_fix(tmp_path, reason):
    api = admit_api(403, {"detail": f"token refused: {reason}", "reason": reason})
    result = run(write_runtime(tmp_path, api), "admit", ADDRESS)
    assert result.exit_code == EXIT_NOT_AUTHORIZED, result.output
    assert REASON_SENTENCES[reason] in result.stderr
    assert FIXES[reason] in result.stderr


def test_no_web_origin_on_the_api_is_exit_4_naming_the_variable(tmp_path):
    """`not_configured` is the deployment's 503, not a refusal: exit 4, with
    its own sentence and fix, not "check STORYDUMP_API and the network"."""
    detail = "the email's claim link cannot be built: set WEB_APP_URL"
    api = admit_api(503, {"detail": detail, "reason": "not_configured"})
    result = run(write_runtime(tmp_path, api), "admit", ADDRESS)
    assert result.exit_code == EXIT_API_UNREACHABLE, result.output
    assert REASON_SENTENCES["not_configured"] in result.stderr
    assert FIXES["not_configured"] in result.stderr
    assert "WEB_APP_URL" in FIXES["not_configured"]


def test_no_web_origin_names_its_reason_in_json(tmp_path):
    api = admit_api(503, {"detail": "x", "reason": "not_configured"})
    result = run(write_runtime(tmp_path, api), "--json", "admit", ADDRESS)
    assert result.exit_code == EXIT_API_UNREACHABLE, result.output
    error = one_envelope(result)["error"]
    assert error["reason"] == "not_configured"
    assert error["fix"] == FIXES["not_configured"]


def test_admit_takes_no_workspace(tmp_path):
    api = admit_api()
    result = run(write_runtime(tmp_path, api), "admit", ADDRESS, "--workspace", WS)
    assert result.exit_code == EXIT_USAGE, result.output
    assert api.calls == []


def test_an_answer_admit_cannot_say_is_exit_4(tmp_path):
    api = admit_api(body={"outcome": "executed"})
    result = run(write_runtime(tmp_path, api), "--json", "admit", ADDRESS)
    assert result.exit_code == EXIT_API_UNREACHABLE, result.output
    assert one_envelope(result)["error"]["reason"] == "api_unreachable"


def test_admit_is_listed_under_writes(tmp_path):
    text = run(write_runtime(tmp_path, write_api()), "--help").stdout
    assert "\n  admit " in text[text.index("Writes:") : text.index("Environment:")]


# --- an interrupted admission (#1740) ---------------------------------------------
#
# An admission carries no idempotency key, so the shared interruption advice
# ("a write's idempotency key makes a re-run safe") is not `admit`'s to give.
# Once the POST may have left, the answer is uncertain: the email may already
# be queued, and a second `--resend` may queue another and replace the link.
# Before it left, nothing was sent.

INTERRUPTIONS = [KeyboardInterrupt, EOFError, click.Abort]
#: The dispatcher's detail for any verb interrupted before it answered, which
#: an interrupted admission keeps (`test_another_write_interrupted_keeps_the_
#: shared_advice` holds the dispatcher to it).
INTERRUPTED_DETAIL = "interrupted before the verb answered"
#: An interruption after the request may have left, by mode.
UNCERTAIN = pytest.mark.parametrize(
    "flags, fix",
    [
        ((), writes.ADMIT_UNCERTAIN_FIX),
        (("--resend",), writes.RESEND_UNCERTAIN_FIX),
    ],
    ids=["admit", "resend"],
)


def interrupting_transport(
    api: Api, interruption=KeyboardInterrupt, at=("POST", ADMISSIONS)
):
    """The scripted API, except that the request *at* is recorded and then
    interrupted, as Ctrl-C while the verb waits for its answer is."""

    def handle(request: httpx.Request) -> httpx.Response:
        if (request.method, request.url.path) == at:
            api.calls.append(request)
            raise interruption()
        return api(request)

    return httpx.MockTransport(handle)


@pytest.mark.parametrize("interruption", INTERRUPTIONS)
@UNCERTAIN
def test_an_admission_interrupted_after_its_post_says_the_answer_is_uncertain(
    tmp_path, flags, fix, interruption
):
    api = write_api()
    rt = write_runtime(tmp_path, api)
    rt.transport = interrupting_transport(api, interruption)
    result = run(rt, "--json", "admit", ADDRESS, *flags)
    assert result.exit_code == EXIT_USAGE, result.output
    (request,) = api.calls
    assert (request.method, request.url.path) == ("POST", ADMISSIONS)
    assert body_of(request) == {"email": ADDRESS, "resend": bool(flags)}
    assert IDEMPOTENCY_HEADER not in request.headers, "an admission carries no key"
    document = one_envelope(result)
    assert document["kind"] == "admit" and document["data"] is None
    assert document["error"] == {
        "code": EXIT_USAGE,
        "reason": "interrupted",
        "detail": INTERRUPTED_DETAIL,
        "fix": fix,
    }


@UNCERTAIN
def test_a_person_reads_the_uncertainty_on_stderr(tmp_path, flags, fix):
    api = write_api()
    rt = write_runtime(tmp_path, api)
    rt.transport = interrupting_transport(api)
    result = run(rt, "admit", ADDRESS, *flags)
    assert result.exit_code == EXIT_USAGE, result.output
    assert result.stdout == ""
    assert result.stderr == f"error: {INTERRUPTED_DETAIL}\nfix: {fix}\n"
    assert len(api.calls) == 1, "nothing is retried on the person's behalf"


@pytest.mark.parametrize("interruption", INTERRUPTIONS)
@pytest.mark.parametrize("seam", ["token", "client"])
@pytest.mark.parametrize("flags", [(), ("--resend",)], ids=["admit", "resend"])
def test_an_admission_interrupted_before_its_post_says_nothing_was_sent(
    tmp_path, monkeypatch, seam, flags, interruption
):
    """Reading the token or building the client sends nothing, so the verb
    can say so — in both modes, and whichever interruption it was."""
    api = write_api()
    rt = write_runtime(tmp_path, api)

    def interrupted(*_args, **_kwargs):
        raise interruption()

    if seam == "token":
        monkeypatch.setattr(rt.token_backend, "get", interrupted)
    else:
        monkeypatch.setattr(rt, "client", interrupted)
    result = run(rt, "--json", "admit", ADDRESS, *flags)
    assert result.exit_code == EXIT_USAGE, result.output
    assert api.calls == []
    error = one_envelope(result)["error"]
    assert (error["reason"], error["detail"], error["fix"]) == (
        "interrupted",
        INTERRUPTED_DETAIL,
        writes.ADMIT_NOT_SENT_FIX,
    )


def test_no_admission_interruption_promises_what_an_admission_does_not_have():
    """No key, no replay, no count of queued emails: an admission has none of
    them, so none of its interruption answers claims one. The words are
    written out here once, since every other test compares against the
    constants; a copy edit changes them here, and the checks below still hold
    it to what an admission can promise."""
    assert writes.ADMIT_NOT_SENT_FIX == (
        "no admission request was sent; run this command again when ready"
    )
    assert writes.ADMIT_UNCERTAIN_FIX == (
        "the answer is uncertain; run ordinary admit again without --resend when"
        " ready. If the address was already admitted, it sends no additional email"
    )
    assert writes.RESEND_UNCERTAIN_FIX == (
        "the answer is uncertain; the email may already be queued. Check storydump"
        " waitlist before deciding to send again; repeating --resend may queue"
        " another email and replace the previous claim link"
    )
    for fix in (
        writes.ADMIT_NOT_SENT_FIX,
        writes.ADMIT_UNCERTAIN_FIX,
        writes.RESEND_UNCERTAIN_FIX,
    ):
        assert "idempotency" not in fix and "key" not in fix, fix
    for fix in (writes.ADMIT_UNCERTAIN_FIX, writes.RESEND_UNCERTAIN_FIX):
        assert fix.startswith("the answer is uncertain"), fix
    assert "may already be queued" in writes.RESEND_UNCERTAIN_FIX
    assert "replace the previous claim link" in writes.RESEND_UNCERTAIN_FIX
    assert "storydump waitlist" in writes.RESEND_UNCERTAIN_FIX


def test_another_write_interrupted_keeps_the_shared_advice(tmp_path):
    """The admission's answers are its own: a command-port write, whose key
    does make a re-run safe, still gets the dispatcher's shared advice — and
    the dispatcher's detail is the one an interrupted admission keeps."""
    command = route(WS, "skip")
    api = write_api()
    rt = write_runtime(tmp_path, api)
    rt.transport = interrupting_transport(api, at=command)
    result = run(rt, "--json", "skip", INTENT, "--workspace", WS)
    assert result.exit_code == EXIT_USAGE, result.output
    error = one_envelope(result)["error"]
    assert (error["reason"], error["detail"], error["fix"]) == (
        "interrupted",
        INTERRUPTED_DETAIL,
        INTERRUPTED_FIX,
    )
    assert IDEMPOTENCY_HEADER in posts(api)[0].headers
