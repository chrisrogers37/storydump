"""Phase 03 of the v2 CLI: the write verbs through the REAL CLI against the
real app AS THE PRODUCTION ROLE — a real workspace created through the API,
real tokens minted through it, real stories on the ledger.

The CLI is a sync HTTP client; the app is ASGI on this test's event loop. A
bridge transport hands each of the CLI's requests back to the loop (the CLI
runs in a worker thread), so what is exercised is the CLI's own client, the
app's own admission, the port's own dedup and audit rows: the deterministic
key replays, a `cli_command` row names the token and the key, a refusal is
the port's reason, a readonly token and a service identity cannot write.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import httpx
import psycopg2
import pytest
from click.testing import CliRunner

from src.services.target.vocabulary import (
    EXIT_API_UNREACHABLE,
    EXIT_NOT_AUTHORIZED,
    EXIT_OK,
    EXIT_REFUSED,
    check_envelope,
)
from storydump_cli.main import Runtime, cli
from storydump_cli.storage import MemoryBackend
from tests.scripts.conftest import seed_intent_chain
from tests.scripts import test_ops_views_gate as ops_gate
from tests.scripts.test_ops_views_gate import _run, _sql
from tests.src.api import conftest as api_conftest
from tests.src.api.conftest import api_client, sign_in

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    # CI runs `tests/scripts` alphabetically and this module is the first to
    # call `asyncio.run()` after modules whose event loops are collected
    # later; under `filterwarnings = error` their `ResourceWarning: unclosed
    # socket` (a loop's self-pipe) surfaces as a PytestUnraisableExceptionWarning
    # inside whichever test the collector runs in — this one, in CI's ordering.
    # This module's own loops, engines, clients and connections are closed
    # explicitly; the warning is another test's. RUN_LOG §7 carries the
    # root-cause follow-up.
    pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning"),
]

#: The ops gate's world — the replayed stream, the ingress login, the bypass
#: login — re-registered here as this module's fixture.
world = ops_gate.world


class LoopBridge(httpx.BaseTransport):
    """A sync transport for the CLI over the app's ASGI transport on *loop*:
    the CLI runs in a worker thread, each request is awaited on the loop."""

    def __init__(self, asgi: httpx.AsyncBaseTransport, loop: asyncio.AbstractEventLoop):
        self.asgi = asgi
        self.loop = loop

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        async def go():
            response = await self.asgi.handle_async_request(request)
            try:
                await response.aread()
            finally:
                await response.aclose()
            return response

        response = asyncio.run_coroutine_threadsafe(go(), self.loop).result(timeout=60)
        return httpx.Response(
            response.status_code, headers=response.headers, content=response.content
        )


def _seed(dsn: str, ws: str, tag: str) -> dict:
    conn = psycopg2.connect(dsn)
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'migration'")
            ids = {
                name: seed_intent_chain(cur, ws, f"{tag}-{name}", state=state)["intent"]
                for name, state in (
                    ("skip", "awaiting_approval"),
                    ("approve", "awaiting_approval"),
                    ("posted", "awaiting_approval"),
                    ("cancel", "awaiting_approval"),
                    ("reader_story", "awaiting_approval"),
                    ("review", "review_required"),
                )
            }
        conn.commit()
        return {k: str(v) for k, v in ids.items()}
    finally:
        conn.close()


@pytest.fixture(scope="module")
def people(world):
    """One workspace through the API, three tokens (operator, readonly, a
    service identity), six stories on its ledger. Module-scoped, so the
    sign-in world is patched here rather than through the function-scoped
    `google_configured` (as the ops gate's `seeded` does)."""
    state: dict = {}

    async def main():
        async with api_client(world["ingress"]) as (client, engine):
            mp = pytest.MonkeyPatch()
            for name, value in (
                ("GOOGLE_CLIENT_ID", api_conftest.CLIENT_ID),
                ("GOOGLE_CLIENT_SECRET", "sec"),
                ("OAUTH_REDIRECT_BASE_URL", api_conftest.API),
                ("WEB_APP_URL", api_conftest.FRONT),
                ("SESSION_COOKIE_DOMAIN", api_conftest.COOKIE_DOMAIN),
            ):
                mp.setattr(api_conftest.settings, name, value, raising=False)
            try:
                owner = await sign_in(
                    client, mp, sub="sub-writes", email="writes@example.test"
                )
                created = await client.post(
                    "/api/v1/workspaces",
                    json={"name": "Writes", "tz": "America/New_York"},
                    headers={**owner, "Idempotency-Key": "create-writes"},
                )
                assert created.status_code == 201, created.text
                ws = created.json()["workspace_id"]
                tokens = {}
                for name, role in (("agent-w", "operator"), ("reader-w", "readonly")):
                    minted = await client.post(
                        "/api/v1/me/tokens",
                        json={"name": name, "role": role},
                        headers=owner,
                    )
                    assert minted.status_code == 201, minted.text
                    tokens[role] = minted.json()["secret"]
                service = await client.post(
                    f"/api/v1/workspaces/{ws}/tokens",
                    json={"name": "svc-w"},
                    headers=owner,
                )
                assert service.status_code == 201, service.text
                state.update(
                    ws=ws,
                    operator=tokens["operator"],
                    readonly=tokens["readonly"],
                    service=service.json()["secret"],
                    **_seed(world["stream"], ws, "w"),
                )
            finally:
                mp.undo()

    _run(main())
    return state


def _runtime(secret: str, bridge: LoopBridge, tmp_path) -> Runtime:
    backend = MemoryBackend()
    backend.set(secret)
    return Runtime(
        config_dir=tmp_path,
        api_url=api_conftest.API,
        token_backend=backend,
        transport=bridge,
        json_mode=True,
        stdin_is_tty=False,
        env={},
    )


async def _cli(rt: Runtime, *args: str):
    """The real CLI, in a worker thread, against the app on this loop."""
    loop = asyncio.get_running_loop()

    def invoke():
        return CliRunner().invoke(cli, list(args), obj=rt, catch_exceptions=False)

    result = await loop.run_in_executor(None, invoke)
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    document = json.loads(lines[-1]) if lines else None
    if document is not None:
        check_envelope(document)
    return result.exit_code, document


def _state(dsn: str, intent_id: str) -> str:
    ((state,),) = _sql(
        dsn, "SELECT state FROM post_intents WHERE id = %s", (intent_id,)
    )
    return state


def test_a_skip_through_the_real_cli_lands_audits_and_replays(world, people, tmp_path):
    ws, story = people["ws"], people["skip"]

    async def main():
        async with api_client(world["ingress"]) as (client, engine):
            bridge = LoopBridge(client._transport, asyncio.get_running_loop())
            rt = _runtime(people["operator"], bridge, tmp_path)

            code, doc = await _cli(rt, "skip", story, "--workspace", ws)
            assert code == EXIT_OK, doc
            assert doc["kind"] == "skip"
            assert doc["data"]["outcome"] == "executed"
            assert doc["data"]["idempotency_key"] == f"skip:{story}"
            assert _state(world["stream"], story) == "skipped"

            # the port's own audit rows: the transition over the `cli` channel
            # and the `cli_command` row naming the token and the key (F4)
            rows = _sql(
                world["stream"],
                "SELECT to_state, channel, detail::text FROM audit_events"
                " WHERE entity_id = %s ORDER BY created_at, id",
                (story,),
            )
            channels = {channel for _, channel, _ in rows}
            assert channels == {"cli"}, rows
            (cli_row,) = [json.loads(d) for _, _, d in rows if d and "cli_command" in d]
            assert cli_row["token_name"] == "agent-w"
            assert cli_row["external_ref"] == f"skip:{story}"
            assert cli_row["kind"] == "skip"

            # a re-run replays: nothing written, exit 0, "already done"
            code, doc = await _cli(rt, "skip", story, "--workspace", ws)
            assert code == EXIT_OK, doc
            assert doc["data"]["outcome"] == "replayed"
            ((dedup,),) = _sql(
                world["stream"],
                "SELECT count(*) FROM command_dedup WHERE external_ref = %s",
                (f"skip:{story}",),
            )
            assert dedup == 1
            assert len(
                _sql(
                    world["stream"],
                    "SELECT 1 FROM audit_events WHERE entity_id = %s",
                    (story,),
                )
            ) == len(rows)

            # a deliberate second execution under its own key is the port's
            # answer for a settled story, not a replay
            code, doc = await _cli(
                rt, "skip", story, "--workspace", ws, "--idempotency-key", "again"
            )
            assert code == EXIT_OK, doc
            assert doc["data"]["outcome"] in ("answered", "executed"), doc

    _run(main())


def test_approve_is_refused_by_the_port_in_manual_mode(world, people, tmp_path):
    ws, story = people["ws"], people["approve"]

    async def main():
        async with api_client(world["ingress"]) as (client, engine):
            bridge = LoopBridge(client._transport, asyncio.get_running_loop())
            rt = _runtime(people["operator"], bridge, tmp_path)
            code, doc = await _cli(rt, "approve", story, "--workspace", ws)
            assert code == EXIT_REFUSED, doc
            assert doc["error"]["reason"] == "manual_mode"
            assert doc["error"]["code"] == EXIT_REFUSED
            assert _state(world["stream"], story) == "awaiting_approval"
            assert not _sql(
                world["stream"],
                "SELECT 1 FROM audit_events WHERE entity_id = %s AND detail::text LIKE %s",
                (story, "%cli_command%"),
            ), "a refusal leaves no cli_command row"

    _run(main())


def test_a_readonly_token_and_a_service_identity_cannot_write(world, people, tmp_path):
    ws, story = people["ws"], people["reader_story"]

    async def main():
        async with api_client(world["ingress"]) as (client, engine):
            bridge = LoopBridge(client._transport, asyncio.get_running_loop())
            for secret in (people["readonly"], people["service"]):
                rt = _runtime(secret, bridge, tmp_path)
                code, doc = await _cli(rt, "skip", story, "--workspace", ws)
                assert code == EXIT_NOT_AUTHORIZED, doc
                assert doc["error"]["reason"] == "readonly_token"
            assert _state(world["stream"], story) == "awaiting_approval"

    _run(main())


def test_posted_cancel_and_resolve_through_the_real_cli(world, people, tmp_path):
    ws = people["ws"]

    async def main():
        async with api_client(world["ingress"]) as (client, engine):
            bridge = LoopBridge(client._transport, asyncio.get_running_loop())
            rt = _runtime(people["operator"], bridge, tmp_path)

            code, doc = await _cli(rt, "posted", people["posted"], "--workspace", ws)
            assert code == EXIT_OK, doc
            assert _state(world["stream"], people["posted"]) == "posted"

            code, doc = await _cli(rt, "cancel", people["cancel"], "--workspace", ws)
            assert code == EXIT_OK, doc
            # the user never writes a terminal state (`02` §4): a cancel FLAGS
            # the story and a door terminalizes it — the flag is the effect
            ((state, flagged),) = _sql(
                world["stream"],
                "SELECT state, cancel_requested FROM post_intents WHERE id = %s",
                (people["cancel"],),
            )
            assert flagged or state == "cancelled", (state, flagged)

            # a retry re-checks approve's gates: this workspace is in manual mode,
            # so the port refuses it — the CLI's sentence, exit 2, nothing written
            code, doc = await _cli(
                rt, "resolve", people["review"], "retry", "--workspace", ws
            )
            assert code == EXIT_REFUSED, doc
            assert doc["error"]["reason"] == "manual_mode"
            assert _state(world["stream"], people["review"]) == "review_required"

            code, doc = await _cli(
                rt, "resolve", people["review"], "cancel", "--workspace", ws
            )
            assert code == EXIT_OK, doc
            assert doc["data"]["args"] == {
                "intent_id": people["review"],
                "resolution": "cancel",
            }
            # the web's identity: the story, the resolution, the review episode
            key = doc["data"]["idempotency_key"]
            assert key.startswith(f"resolve_review:{people['review']}:cancel:20"), key
            assert _state(world["stream"], people["review"]) == "cancelled"

    _run(main())


def test_pause_and_resume_the_workspace(world, people, tmp_path):
    ws = people["ws"]

    async def main():
        async with api_client(world["ingress"]) as (client, engine):
            bridge = LoopBridge(client._transport, asyncio.get_running_loop())
            rt = _runtime(people["operator"], bridge, tmp_path)
            # pause → resume → pause: every one EXECUTES (a fresh key per
            # invocation — a day-keyed pause replayed the morning's and left the
            # workspace resumed, the review round's blocker), and the last one lands
            for verb, expected in (("pause", True), ("resume", False), ("pause", True)):
                code, doc = await _cli(rt, verb, "--workspace", ws)
                assert code == EXIT_OK, doc
                assert doc["data"]["outcome"] == "executed", (verb, doc)
                assert doc["data"]["result"] == {"is_paused": expected}
                ((paused,),) = _sql(
                    world["stream"],
                    "SELECT is_paused FROM workspaces WHERE id = %s",
                    (ws,),
                )
                assert paused is expected, verb
            code, doc = await _cli(rt, "resume", "--workspace", ws)
            assert code == EXIT_OK and doc["data"]["outcome"] == "executed", doc

    _run(main())


def test_health_through_the_real_cli_reports_the_three_surfaces(
    world, people, tmp_path
):
    async def main():
        async with api_client(world["ingress"]) as (client, engine):
            bridge = LoopBridge(client._transport, asyncio.get_running_loop())
            rt = _runtime(people["operator"], bridge, tmp_path)
            code, doc = await _cli(rt, "health")
            assert doc["kind"] == "health", doc
            data = doc["data"]
            assert data["api"]["status"] == "ok"
            assert "db_role" in data["api"] and "taps" in data["api"]
            assert set(data) == {"ok", "api", "scheduling", "posting", "verdicts"}
            assert set(data["verdicts"]) == {"api", "scheduling", "posting"}
            # no worker heartbeat on a replayed database: the verdict is the
            # exit code, the report is still the answer
            assert code == (EXIT_OK if data["ok"] else EXIT_API_UNREACHABLE)

    _run(main())


def test_a_workspace_name_resolves_through_the_real_principal(world, people, tmp_path):
    async def main():
        async with api_client(world["ingress"]) as (client, engine):
            bridge = LoopBridge(client._transport, asyncio.get_running_loop())
            rt = _runtime(people["operator"], bridge, tmp_path)
            code, doc = await _cli(rt, "pause", "--workspace", "Writes")
            assert code == EXIT_OK, doc
            assert doc["data"]["outcome"] == "executed", (
                "not a replay of the earlier pause"
            )
            assert doc["data"]["workspace_id"] == people["ws"]
            code, doc = await _cli(rt, "resume", "--workspace", "Writes")
            assert code == EXIT_OK, doc
            # a stranger's workspace id is the API's 404 — not a member
            code, doc = await _cli(rt, "pause", "--workspace", str(uuid.uuid4()))
            assert code == EXIT_NOT_AUTHORIZED, doc

    _run(main())
