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

import httpx
import pytest

from src.services.target.vocabulary import (
    EXIT_API_UNREACHABLE,
    EXIT_NOT_AUTHORIZED,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_USAGE,
    OUTCOME_SENTENCES,
    REASON_SENTENCES,
    TAP_WORDS,
    WRITE_SENTENCES,
)
from storydump_cli.commands import writes
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
        {"id": WS, "name": "Chris's studio", "role": "owner"},
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
                409,
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
    result = run(write_runtime(tmp_path, api), "sync", "memes", "--workspace", WS)
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
    }
    assert writes.COMMAND_OF["posted"] == "mark_posted"
    assert writes.COMMAND_OF["resolve"] == "resolve_review"
    assert writes.COMMAND_OF["sync"] == "sync_now"
