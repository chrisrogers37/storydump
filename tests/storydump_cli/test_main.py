"""The verbs end to end, against a scripted API and an in-memory token store.

Everything goes through ``main``/``cli.main`` so the exit codes under test
are the CLI's own (spec §6: Click's are not exposed): 64 for usage, 3 for
not authorized, 4 for an API that did not answer, 1 for a token that does
not exist. ``Runtime`` is built by hand and passed as ``obj`` so no test
touches a real keychain, the home directory or the network.
"""

from __future__ import annotations

import json
import stat

import httpx
import pytest
from click.testing import CliRunner

from src.services.target.vocabulary import (
    EXIT_API_UNREACHABLE,
    EXIT_NOT_AUTHORIZED,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_USAGE,
    REASON_SENTENCES,
    check_envelope,
)
from storydump_cli.config import DEFAULT_API_URL, read_config
from storydump_cli.main import Runtime, build_runtime, cli, main
from storydump_cli.storage import FileBackend, MemoryBackend, token_path

SECRET = "sdt_" + "a" * 43
WS = "11111111-1111-4111-8111-111111111111"
USER = "22222222-2222-4222-8222-222222222222"
TOKEN_ID = "33333333-3333-4333-8333-333333333333"
OTHER_ID = "44444444-4444-4444-8444-444444444444"

PERSON = {
    "kind": "token",
    "user_id": USER,
    "token": {
        "id": TOKEN_ID,
        "name": "chris-mbp",
        "role": "operator",
        "workspace_id": None,
        "expires_at": "2026-12-14T00:00:00Z",
    },
    "workspaces": [{"id": WS, "name": "Chris's studio", "role": "owner"}],
}

SERVICE = {
    "kind": "token",
    "user_id": None,
    "token": {
        "id": TOKEN_ID,
        "name": "grafana",
        "role": "readonly",
        "workspace_id": WS,
        "expires_at": None,
    },
    "workspaces": [{"id": WS, "name": "Chris's studio", "role": "readonly"}],
}


def _token_row(token_id, name, **overrides):
    row = {
        "id": token_id,
        "name": name,
        "role": "operator",
        "expires_at": "2026-12-14T00:00:00Z",
        "revoked_at": None,
        "last_used_at": "2026-09-15T10:00:00Z",
        "created_at": "2026-09-15T09:00:00Z",
    }
    row.update(overrides)
    return row


TOKENS = {
    "tokens": [
        _token_row(TOKEN_ID, "chris-mbp"),
        _token_row(OTHER_ID, "old-laptop", revoked_at="2026-09-01T00:00:00Z"),
    ]
}


class Api:
    """A scripted API: ``(method, path)`` → ``(status, body)`` or an exception
    to raise from the transport; anything unscripted answers 404 as the real
    API does for what a principal cannot see."""

    def __init__(self, routes):
        self.routes = dict(routes)
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        answer = self.routes.get((request.method, request.url.path))
        if answer is None:
            return httpx.Response(404, json={"detail": "not found"})
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        return httpx.Response(status, json=body)

    @property
    def transport(self) -> httpx.BaseTransport:
        return httpx.MockTransport(self)

    def paths(self, method=None):
        return [r.url.path for r in self.calls if method is None or r.method == method]


def person_api(**overrides) -> Api:
    routes = {
        ("GET", "/api/v1/me/principal"): (200, PERSON),
        ("GET", "/api/v1/me/tokens"): (200, TOKENS),
        ("DELETE", f"/api/v1/me/tokens/{TOKEN_ID}"): (200, {"revoked": True}),
        ("DELETE", f"/api/v1/me/tokens/{OTHER_ID}"): (200, {"revoked": True}),
    }
    routes.update(overrides)
    return Api(routes)


def service_api() -> Api:
    return Api(
        {
            ("GET", "/api/v1/me/principal"): (200, SERVICE),
            ("GET", f"/api/v1/workspaces/{WS}/tokens"): (
                200,
                {
                    "tokens": [
                        _token_row(
                            TOKEN_ID, "grafana", role="readonly", workspace_id=WS
                        )
                    ]
                },
            ),
            ("DELETE", f"/api/v1/workspaces/{WS}/tokens/{TOKEN_ID}"): (
                200,
                {"revoked": True},
            ),
        }
    )


def runtime(tmp_path, api: Api, *, token=SECRET, env=None) -> Runtime:
    backend = MemoryBackend()
    if token:
        backend.set(token)
    return Runtime(
        config_dir=tmp_path,
        api_url="https://api.test",
        token_backend=backend,
        transport=api.transport,
        json_mode=False,
        stdin_is_tty=False,
        env=dict(env or {}),
    )


def run(rt: Runtime, *args, input=None):
    return CliRunner().invoke(
        cli, list(args), input=input, obj=rt, catch_exceptions=False
    )


def one_envelope(result):
    lines = result.stdout.splitlines()
    assert len(lines) == 1, result.stdout
    document = json.loads(lines[0])
    check_envelope(document)
    return document


# --- the entry point ---------------------------------------------------------


def test_a_bad_option_is_usage_on_stderr_and_exit_64(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("STORYDUMP_CONFIG_DIR", str(tmp_path))
    assert main(["--bogus"]) == EXIT_USAGE
    out, err = capsys.readouterr()
    assert out == ""
    assert "Usage: storydump" in err
    assert "--bogus" in err


def test_a_usage_error_after_json_is_still_an_envelope(tmp_path):
    """`--json` is honoured wherever it stands: a bad value parsed BEFORE the
    flag's callback ran is still one envelope on stdout, exit 64 — an agent
    reads one stream whatever went wrong."""
    api = person_api()
    result = run(runtime(tmp_path, api), "floating", "--limit", "abc", "--json")
    assert result.exit_code == EXIT_USAGE, result.output
    document = one_envelope(result)
    assert document["kind"] == "floating" and document["error"]["reason"] == "usage"
    assert "abc" in document["error"]["detail"]
    result = run(runtime(tmp_path, api), "--bogus", "--json")
    assert result.exit_code == EXIT_USAGE
    assert one_envelope(result)["error"]["reason"] == "usage"


def test_an_interrupt_outside_a_watch_is_exit_64_with_an_envelope(
    tmp_path, monkeypatch
):
    """Ctrl-C before a verb answered: the documented usage code, and a document
    that says so — not Click's bare `Aborted!` and no envelope. (Inside a
    watch, Ctrl-C is the way to stop watching: 0, pinned in test_watch.)"""
    rt = runtime(tmp_path, person_api())

    def interrupted():
        raise KeyboardInterrupt

    monkeypatch.setattr(rt.token_backend, "get", interrupted)
    result = run(rt, "--json", "whoami")
    assert result.exit_code == EXIT_USAGE, result.output
    document = one_envelope(result)
    assert document["kind"] == "whoami" and document["error"]["reason"] == "interrupted"
    assert "idempotency" in document["error"]["fix"]
    rt = runtime(tmp_path, person_api())
    monkeypatch.setattr(rt.token_backend, "get", interrupted)
    result = run(rt, "whoami")
    assert result.exit_code == EXIT_USAGE
    assert "interrupted" in result.stderr


def test_a_closed_pipe_is_not_a_failure(tmp_path, monkeypatch):
    """`storydump … | head` closes stdout early; the CLI ends quietly with 0
    rather than a traceback and Click's exit 1 (the contract's "not found")."""
    import errno

    rt = runtime(tmp_path, person_api())

    def closed(document, *, json_mode):
        # the real shape: Click's own `main` would catch this EPIPE and
        # `sys.exit(1)` before any arm of ours saw it
        raise BrokenPipeError(errno.EPIPE, "Broken pipe")

    monkeypatch.setattr("storydump_cli.commands.auth.emit", closed)
    result = run(rt, "--json", "whoami")
    assert result.exit_code == EXIT_OK, result.output
    assert "Traceback" not in result.output


def test_help_and_version_still_exit_0_through_the_dispatcher(tmp_path):
    """`dispatch` runs Click's context itself now; Click's own clean exits
    (`--help`, `--version`) must still be 0."""
    rt = runtime(tmp_path, person_api())
    assert run(rt, "--help").exit_code == EXIT_OK
    assert run(rt, "--version").exit_code == EXIT_OK
    assert run(rt, "whoami", "--help").exit_code == EXIT_OK


def test_a_config_directory_that_cannot_be_written_is_usage_not_a_traceback(tmp_path):
    blocker = tmp_path / "config"
    blocker.write_text("a file where the directory should be")
    rt = runtime(tmp_path / "config", person_api(), token=None)
    result = run(rt, "--json", "login", input=SECRET + "\n")
    assert result.exit_code == EXIT_USAGE, result.output
    document = one_envelope(result)
    assert document["error"]["reason"] == "usage"
    assert str(blocker) in document["error"]["detail"]
    assert SECRET not in result.output


def test_a_saturated_pool_answer_is_exit_4_with_its_own_sentence(tmp_path):
    """The API sheds load with a 503 whose body names `pool_saturated`
    (`src/api/app.py`): a 5xx, so exit 4 — but the reason's own sentence and
    fix, not "check STORYDUMP_API and the network"."""
    api = person_api()
    api.routes[("GET", "/api/v1/me/principal")] = (
        503,
        {"detail": "busy — try again", "reason": "pool_saturated"},
    )
    result = run(runtime(tmp_path, api), "--json", "whoami")
    assert result.exit_code == EXIT_API_UNREACHABLE, result.output
    document = one_envelope(result)
    assert document["error"]["code"] == EXIT_API_UNREACHABLE
    assert document["error"]["reason"] == "pool_saturated"
    assert "try again" in document["error"]["detail"].lower()
    assert "network" not in document["error"]["fix"]
    # a 5xx with no reason keeps the unreachable answer
    api.routes[("GET", "/api/v1/me/principal")] = (502, {"detail": "bad gateway"})
    result = run(runtime(tmp_path, api), "--json", "whoami")
    assert one_envelope(result)["error"]["reason"] == "api_unreachable"


def test_no_verb_is_usage(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("STORYDUMP_CONFIG_DIR", str(tmp_path))
    assert main([]) == EXIT_USAGE
    assert "Usage: storydump" in capsys.readouterr().err


def test_a_corrupt_config_file_is_usage_with_a_fix(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("STORYDUMP_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.json").write_text("{not json")
    assert main(["whoami"]) == EXIT_USAGE
    err = capsys.readouterr().err
    assert "config.json" in err
    assert "fix:" in err


def test_a_config_value_outside_its_set_is_usage(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"token_storage": "dropbox"}))
    result = run(runtime(tmp_path, person_api()), "--json", "whoami")
    assert result.exit_code == EXIT_USAGE
    document = one_envelope(result)
    assert document["error"]["reason"] == "usage"
    assert "dropbox" in document["error"]["detail"]


def test_help_documents_the_exit_codes(tmp_path):
    result = run(runtime(tmp_path, person_api()), "--help")
    assert result.exit_code == EXIT_OK
    for code in ("0", "1", "2", "3", "4", "64"):
        assert code in result.stdout
    assert "sdt_" + "a" * 8 not in result.stdout


def test_build_runtime_precedence_env_over_config_over_default(tmp_path, monkeypatch):
    rt = build_runtime({"STORYDUMP_CONFIG_DIR": str(tmp_path)})
    assert rt.api_url == DEFAULT_API_URL
    assert rt.config_dir == tmp_path
    assert rt.token_backend is None, (
        "the keychain is resolved lazily, from the final host"
    )
    (tmp_path / "config.json").write_text(
        json.dumps({"api_url": "https://from.config"})
    )
    assert (
        build_runtime({"STORYDUMP_CONFIG_DIR": str(tmp_path)}).api_url
        == "https://from.config"
    )
    rt = build_runtime(
        {"STORYDUMP_CONFIG_DIR": str(tmp_path), "STORYDUMP_API": "https://from.env"}
    )
    assert rt.api_url == "https://from.env"


def test_api_flag_wins_over_everything(tmp_path):
    api = person_api()
    result = run(runtime(tmp_path, api), "--api", "https://flag.test", "whoami")
    assert result.exit_code == EXIT_OK, result.output
    assert api.calls[0].url.host == "flag.test"


# --- login -------------------------------------------------------------------


def test_login_from_stdin_verifies_stores_and_names_the_token(tmp_path):
    api = person_api()
    rt = runtime(tmp_path, api, token=None)
    result = run(rt, "login", input=f"{SECRET}\n")
    assert result.exit_code == EXIT_OK, result.output
    assert api.calls[0].headers["authorization"] == f"Bearer {SECRET}"
    assert rt.token_backend.get() == SECRET
    assert "signed in as chris-mbp (operator)" in result.stdout
    assert "Chris's studio" in result.stdout
    assert SECRET not in result.output
    assert read_config(tmp_path).token_storage == "keychain"
    assert read_config(tmp_path).api_url == "https://api.test"


def test_login_json_is_an_envelope_of_kind_login(tmp_path):
    result = run(
        runtime(tmp_path, person_api(), token=None), "--json", "login", input=SECRET
    )
    assert result.exit_code == EXIT_OK, result.output
    document = one_envelope(result)
    assert document["kind"] == "login"
    assert document["data"]["token"]["name"] == "chris-mbp"


def test_login_refuses_a_value_that_is_not_a_token(tmp_path):
    api = person_api()
    rt = runtime(tmp_path, api, token=None)
    result = run(rt, "login", input="ghp_notours\n")
    assert result.exit_code == EXIT_USAGE
    assert "not a storydump token" in result.stderr
    assert "Settings" in result.stderr
    assert api.calls == [], "a value that is not ours never reaches the API"
    assert rt.token_backend.get() is None


def test_login_refuses_an_empty_secret(tmp_path):
    result = run(runtime(tmp_path, person_api(), token=None), "login", input="\n")
    assert result.exit_code == EXIT_USAGE


def test_login_with_a_refused_token_exits_3(tmp_path):
    api = person_api()
    api.routes[("GET", "/api/v1/me/principal")] = (
        401,
        {"detail": "authentication required"},
    )
    rt = runtime(tmp_path, api, token=None)
    result = run(rt, "login", input=SECRET)
    assert result.exit_code == EXIT_NOT_AUTHORIZED
    assert rt.token_backend.get() is None, "a refused token is not stored"
    assert "error:" in result.stderr


def test_login_insecure_storage_writes_a_0600_file_and_records_it(tmp_path):
    api = person_api()
    rt = runtime(tmp_path, api, token=None)
    result = run(rt, "login", "--insecure-storage", input=SECRET)
    assert result.exit_code == EXIT_OK, result.output
    path = token_path(tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text().strip() == SECRET
    assert read_config(tmp_path).token_storage == "file"
    assert rt.token_backend.get() is None, "the keychain is untouched"
    # and the file is where the next verb reads from
    result = run(rt, "whoami")
    assert result.exit_code == EXIT_OK, result.output
    assert api.calls[-1].headers["authorization"] == f"Bearer {SECRET}"


def test_login_to_the_keychain_removes_a_stale_file(tmp_path):
    rt = runtime(tmp_path, person_api(), token=None)
    FileBackend(token_path(tmp_path)).set("sdt_" + "o" * 43)
    result = run(rt, "login", input=SECRET)
    assert result.exit_code == EXIT_OK, result.output
    assert not token_path(tmp_path).exists()
    assert read_config(tmp_path).token_storage == "keychain"


def test_login_refuses_while_the_variable_is_set(tmp_path):
    api = person_api()
    rt = runtime(tmp_path, api, token=None, env={"STORYDUMP_TOKEN": SECRET})
    result = run(rt, "login", input=SECRET)
    assert result.exit_code == EXIT_USAGE
    assert "STORYDUMP_TOKEN" in result.stderr
    assert api.calls == []


def test_login_prompts_on_a_tty(tmp_path):
    rt = runtime(tmp_path, person_api(), token=None)
    rt.stdin_is_tty = True
    result = run(rt, "login", input=f"{SECRET}\n")
    assert result.exit_code == EXIT_OK, result.output
    assert "API token" in result.output
    assert SECRET not in result.output


# --- whoami ------------------------------------------------------------------


def test_whoami_json_is_one_envelope_of_kind_whoami(tmp_path):
    result = run(runtime(tmp_path, person_api()), "whoami", "--json")
    assert result.exit_code == EXIT_OK, result.output
    document = one_envelope(result)
    assert document["kind"] == "whoami"
    assert document["data"] == PERSON


def test_whoami_human_names_the_token_and_each_workspace(tmp_path):
    result = run(runtime(tmp_path, person_api()), "whoami")
    assert result.exit_code == EXIT_OK, result.output
    assert "chris-mbp" in result.stdout
    assert "operator" in result.stdout
    assert "Chris's studio" in result.stdout
    assert "owner" in result.stdout


def test_whoami_with_a_401_exits_3_with_the_sentence(tmp_path):
    api = person_api()
    api.routes[("GET", "/api/v1/me/principal")] = (
        401,
        {"detail": "authentication required"},
    )
    result = run(runtime(tmp_path, api), "whoami")
    assert result.exit_code == EXIT_NOT_AUTHORIZED
    assert REASON_SENTENCES["not_authorized"] in result.stderr
    assert result.stdout == ""


def test_whoami_json_error_is_an_envelope_on_stdout(tmp_path):
    api = person_api()
    api.routes[("GET", "/api/v1/me/principal")] = (
        401,
        {"detail": "authentication required"},
    )
    result = run(runtime(tmp_path, api), "--json", "whoami")
    assert result.exit_code == EXIT_NOT_AUTHORIZED
    document = one_envelope(result)
    assert document["kind"] == "whoami"
    assert document["error"]["code"] == EXIT_NOT_AUTHORIZED
    assert document["error"]["reason"] == "not_authorized"


def test_a_403_with_a_reason_uses_the_reason_sentence(tmp_path):
    api = person_api()
    api.routes[("GET", "/api/v1/me/principal")] = (
        403,
        {"detail": "session only", "reason": "session_required"},
    )
    result = run(runtime(tmp_path, api), "whoami")
    assert result.exit_code == EXIT_NOT_AUTHORIZED
    assert REASON_SENTENCES["session_required"] in result.stderr


def test_whoami_without_a_token_exits_3_without_calling_the_api(tmp_path):
    api = person_api()
    result = run(runtime(tmp_path, api, token=None), "whoami")
    assert result.exit_code == EXIT_NOT_AUTHORIZED
    assert "storydump login" in result.stderr
    assert api.calls == []


def test_the_variable_is_used_when_set(tmp_path):
    api = person_api()
    from_env = "sdt_" + "e" * 43
    result = run(runtime(tmp_path, api, env={"STORYDUMP_TOKEN": from_env}), "whoami")
    assert result.exit_code == EXIT_OK, result.output
    assert api.calls[0].headers["authorization"] == f"Bearer {from_env}"


def test_an_unreachable_api_exits_4(tmp_path):
    api = person_api()
    api.routes[("GET", "/api/v1/me/principal")] = httpx.ConnectError(
        "connection refused"
    )
    result = run(runtime(tmp_path, api), "whoami")
    assert result.exit_code == EXIT_API_UNREACHABLE
    assert "STORYDUMP_API" in result.stderr


def test_a_5xx_exits_4(tmp_path):
    api = person_api()
    api.routes[("GET", "/api/v1/me/principal")] = (502, {"detail": "bad gateway"})
    result = run(runtime(tmp_path, api), "--json", "whoami")
    assert result.exit_code == EXIT_API_UNREACHABLE
    assert one_envelope(result)["error"]["code"] == EXIT_API_UNREACHABLE


# --- tokens ------------------------------------------------------------------


def test_tokens_list_json_for_a_person_reads_me_tokens(tmp_path):
    api = person_api()
    result = run(runtime(tmp_path, api), "tokens", "list", "--json")
    assert result.exit_code == EXIT_OK, result.output
    document = one_envelope(result)
    assert document["kind"] == "tokens"
    assert [row["name"] for row in document["data"]["tokens"]] == [
        "chris-mbp",
        "old-laptop",
    ]
    assert api.paths("GET") == ["/api/v1/me/principal", "/api/v1/me/tokens"]


def test_tokens_list_human_is_a_table(tmp_path):
    result = run(runtime(tmp_path, person_api()), "tokens", "list")
    assert result.exit_code == EXIT_OK, result.output
    for word in (
        "id",
        "name",
        "role",
        "expires",
        "last used",
        "revoked",
        TOKEN_ID,
        "old-laptop",
    ):
        assert word in result.stdout, word


def test_tokens_list_for_a_service_identity_reads_its_workspace(tmp_path):
    api = service_api()
    result = run(runtime(tmp_path, api), "--json", "tokens", "list")
    assert result.exit_code == EXIT_OK, result.output
    assert one_envelope(result)["data"]["tokens"][0]["name"] == "grafana"
    assert api.paths("GET")[-1] == f"/api/v1/workspaces/{WS}/tokens"


def test_tokens_revoke_deletes_and_names_the_token(tmp_path):
    api = person_api()
    result = run(runtime(tmp_path, api), "tokens", "revoke", OTHER_ID)
    assert result.exit_code == EXIT_OK, result.output
    assert "revoked old-laptop" in result.stdout
    assert api.paths("DELETE") == [f"/api/v1/me/tokens/{OTHER_ID}"]


def test_tokens_revoke_json(tmp_path):
    result = run(
        runtime(tmp_path, person_api()), "--json", "tokens", "revoke", OTHER_ID
    )
    assert result.exit_code == EXIT_OK, result.output
    document = one_envelope(result)
    assert document["kind"] == "tokens"
    assert document["data"]["revoked"] == {"id": OTHER_ID, "name": "old-laptop"}


def test_tokens_revoke_for_a_service_identity_uses_the_workspace_route(tmp_path):
    api = service_api()
    result = run(runtime(tmp_path, api), "tokens", "revoke", TOKEN_ID)
    assert result.exit_code == EXIT_OK, result.output
    assert api.paths("DELETE") == [f"/api/v1/workspaces/{WS}/tokens/{TOKEN_ID}"]


def test_tokens_revoke_unknown_id_exits_1(tmp_path):
    api = person_api()
    result = run(
        runtime(tmp_path, api),
        "tokens",
        "revoke",
        "55555555-5555-4555-8555-555555555555",
    )
    assert result.exit_code == EXIT_NOT_FOUND
    assert "no such token" in result.stderr
    assert api.paths("DELETE") == []


def test_tokens_revoke_raced_404_exits_1(tmp_path):
    api = person_api()
    api.routes[("DELETE", f"/api/v1/me/tokens/{OTHER_ID}")] = (
        404,
        {"detail": "not found"},
    )
    result = run(runtime(tmp_path, api), "--json", "tokens", "revoke", OTHER_ID)
    assert result.exit_code == EXIT_NOT_FOUND
    assert one_envelope(result)["error"]["reason"] == "not_found"


def test_tokens_revoke_refused_by_the_api_exits_3(tmp_path):
    api = person_api()
    api.routes[("DELETE", f"/api/v1/me/tokens/{OTHER_ID}")] = (
        403,
        {"detail": "not yours", "reason": "wrong_workspace"},
    )
    result = run(runtime(tmp_path, api), "tokens", "revoke", OTHER_ID)
    assert result.exit_code == EXIT_NOT_AUTHORIZED
    assert REASON_SENTENCES["wrong_workspace"] in result.stderr


def test_there_is_no_mint_verb(tmp_path):
    result = run(runtime(tmp_path, person_api()), "tokens", "mint")
    assert result.exit_code == EXIT_USAGE


# --- logout ------------------------------------------------------------------


def test_logout_forgets_the_token_and_the_file(tmp_path):
    api = person_api()
    rt = runtime(tmp_path, api)
    FileBackend(token_path(tmp_path)).set(SECRET)
    result = run(rt, "logout")
    assert result.exit_code == EXIT_OK, result.output
    assert "signed out" in result.stdout
    assert rt.token_backend.get() is None
    assert not token_path(tmp_path).exists()
    assert api.calls == []
    # a second logout is not an error
    assert run(rt, "logout").exit_code == EXIT_OK


def test_logout_json(tmp_path):
    result = run(runtime(tmp_path, person_api()), "--json", "logout")
    assert one_envelope(result)["kind"] == "logout"


# --- every verb has help with an example ------------------------------------


@pytest.mark.parametrize(
    "verb",
    [
        ("login",),
        ("logout",),
        ("whoami",),
        ("tokens",),
        ("tokens", "list"),
        ("tokens", "revoke"),
    ],
    ids=lambda v: " ".join(v),
)
def test_every_verb_has_help_with_an_example(tmp_path, verb):
    result = run(runtime(tmp_path, person_api()), *verb, "--help")
    assert result.exit_code == EXIT_OK, result.output
    assert "storydump " + " ".join(verb) in result.stdout
    assert "Example" in result.stdout, "every verb's help shows one example"


def test_the_api_flag_refuses_plain_http_off_this_machine(tmp_path):
    result = run(
        runtime(tmp_path, person_api()), "--api", "http://api.example.com", "whoami"
    )
    assert result.exit_code == EXIT_USAGE, result.output
    assert "plain http" in result.output and "STORYDUMP_INSECURE_HTTP" in result.output


def test_the_insecure_http_variable_allows_a_dev_server_elsewhere(tmp_path):
    api = person_api()
    rt = runtime(tmp_path, api, env={"STORYDUMP_INSECURE_HTTP": "1"})
    result = run(rt, "--api", "http://api.example.com", "whoami")
    assert result.exit_code == EXIT_OK, result.output


def test_a_file_login_retires_the_keychain_copy(tmp_path):
    """One stored token, never two: a keychain login followed by
    `login --insecure-storage` leaves nothing in the keychain."""
    api = person_api()
    rt = runtime(tmp_path, api, token="sdt_" + "o" * 43)
    assert rt.token_backend.get() is not None
    result = run(rt, "login", "--insecure-storage", input=f"{SECRET}\n")
    assert result.exit_code == EXIT_OK, result.output
    assert rt.token_backend.get() is None
    assert FileBackend(token_path(tmp_path)).get() == SECRET
    assert read_config(tmp_path).token_storage == "file"


def test_login_with_no_usable_keychain_fails_closed_with_the_fix(tmp_path, monkeypatch):
    """F2 (d) end to end: keyring chose its `fail` backend (a headless host),
    so login stores nothing and names the two ways out."""
    import sys
    import types

    fail = types.ModuleType("keyring")
    backend = type("Keyring", (), {"__module__": "keyring.backends.fail"})()
    fail.get_keyring = lambda: backend
    monkeypatch.setitem(sys.modules, "keyring", fail)
    api = person_api()
    rt = runtime(tmp_path, api, token=None)
    rt.token_backend = None  # the real keychain path, resolved lazily
    result = run(rt, "login", input=f"{SECRET}\n")
    assert result.exit_code == EXIT_NOT_AUTHORIZED, result.output
    assert "--insecure-storage" in result.output and "STORYDUMP_TOKEN" in result.output
    assert not token_path(tmp_path).exists()


@pytest.mark.parametrize("value", ["0", "", "no", "false"])
def test_the_insecure_http_variable_needs_a_true_word(tmp_path, value):
    rt = runtime(tmp_path, person_api(), env={"STORYDUMP_INSECURE_HTTP": value})
    result = run(rt, "--api", "http://api.example.com", "whoami")
    assert result.exit_code == EXIT_USAGE, result.output
    assert "https URL" in result.output and "STORYDUMP_INSECURE_HTTP=1" in result.output
