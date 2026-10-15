"""The isolated provider gate's pre-connection guard (`tests/isolation_guard.py`).

No database and no network: the deny guard is exercised over recording stubs
that stand where the real socket, libpq and httpx entry points do, and the
plugin is exercised in subprocesses whose entry points are replaced by
`tests/_isolation_spy.py`, which logs and then blocks. A refusal is therefore
proven by an EMPTY spy log, and the spy is proven able to fill by the control
runs below that the guard does not stand in front of.

The subprocess runs use a throwaway rootdir that symlinks this checkout's
`tests/` and `pytest.ini`. The guard refuses a `.env` at the rootdir, so
running against the real checkout would make these tests depend on whether
the developer running them keeps one.
"""

import asyncio
import importlib
import os
import re
import socket
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import httpx
import psycopg2
import pytest
from cryptography.fernet import Fernet

from tests import isolation_guard as guard

REPO = Path(__file__).resolve().parents[1]

#: The file the refusal cases and the collect-only control name, as the gate's
#: recipe names the gate: the real full-composition gate. Only refusals and a
#: collection are run against it here, never a test of it.
GATE_TARGET = "tests/scripts/test_instagram_source_gate.py"

#: A file that passes with no database, for the controls that RUN a test: the
#: real gate would run for real under a blocked probe.
CONTROL_TARGET = "tests/scripts/isolation_control_target.py"

#: A configuration the guard accepts. Test-only values; the key is fresh per run.
GOOD = {
    "DB_HOST": "localhost",
    "DB_PORT": "65433",
    "DB_USER": "test_user",
    "DB_PASSWORD": "test_password",
    "DB_NAME": "storyline_ai",
    "TEST_DB_NAME": "storyline_test",
    "REQUIRE_TEST_DATABASE": "1",
    "ENCRYPTION_KEY": Fernet.generate_key().decode(),
}

#: The same without `REQUIRE_TEST_DATABASE`, for runs that let the root fixture
#: probe the (spied, blocked) database: with it set, "no server answered" is a
#: failure by design, and these runs are after what the probe reaches.
GOOD_WITHOUT_A_DATABASE = {
    k: v for k, v in GOOD.items() if k != "REQUIRE_TEST_DATABASE"
}


def clean_env(**extra):
    """A child's whole environment: nothing inherited but PATH. No bytecode is
    written: pytest's assertion-rewrite cache keeps the file name of the run
    that wrote it, and a run through a temporary rootdir would leave that name
    in this checkout's `__pycache__` for every later traceback."""
    return {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": str(REPO),
        "PYTHONDONTWRITEBYTECODE": "1",
        **extra,
    }


# --- the effective-configuration validator ---------------------------------------------


@pytest.fixture
def armed(monkeypatch):
    """The gate armed, with a fresh refusal ledger."""
    monkeypatch.setattr(guard, "ACTIVE", True)
    monkeypatch.setattr(guard, "VIOLATIONS", [])


def refusal(env=None):
    """The validator's refusal message for *env*, or None if it accepts."""
    try:
        guard.validate_effective_configuration(GOOD if env is None else env)
    except pytest.UsageError as exc:
        return str(exc)
    return None


def named(message):
    """The names a refusal lists, one per `  NAME: reason` line."""
    return set(re.findall(r"^  ([A-Za-z0-9_.]+): ", message, re.M))


LEAK_URL = "postgresql://leak_user:leak-secret@db.example.invalid:5432/prod_like"
LEAK_PIECES = ("leak_user", "leak-secret", "db.example.invalid", "prod_like")

#: (variable, the hostile value, fragments that must never be printed)
REFUSED = [
    ("DB_HOST", "db.example.invalid", ("db.example.invalid",)),
    ("DB_HOST", "10.0.0.5", ("10.0.0.5",)),
    ("DB_PORT", "5432", ("5432",)),
    ("DB_PORT", "not-a-port", ("not-a-port",)),
    ("DB_USER", "storydump_user", ("storydump_user",)),
    ("DB_NAME", "storydump", ("storydump",)),
    ("TEST_DB_NAME", "storydump_test", ("storydump_test",)),
    ("ENCRYPTION_KEY", "not-a-fernet-key", ("not-a-fernet-key",)),
    ("ENCRYPTION_KEYS", "leak-key-1,leak-key-2", ("leak-key",)),
    ("TARGET_DATABASE_URL", LEAK_URL, LEAK_PIECES),
    (
        "DATABASE_URL",
        LEAK_URL.replace("@db.example.invalid", "@localhost"),
        LEAK_PIECES,
    ),
    (
        "TARGET_DATABASE_URL",
        LEAK_URL.replace(":5432", ":65433"),  # the right port on the wrong host
        LEAK_PIECES,
    ),
    (
        "DATABASE_URL",
        "postgresql://leak_user:leak-secret@127.0.0.1/prod_like",
        LEAK_PIECES,
    ),
    (
        "TEST_DATABASE_URL",
        "postgresql://leak_user:leak-secret@localhost:65433/db?host=db.example.invalid",
        LEAK_PIECES,
    ),
    (
        "SCHEMA_DRIFT_DSN",
        "host=db.example.invalid port=65433 user=leak_user password=leak-secret",
        LEAK_PIECES,
    ),
    (
        "TARGET_DATABASE_URL",
        "postgresql://leak_user:leak-secret@db.example.invalid@localhost:65433/prod_like",
        LEAK_PIECES,
    ),
    ("PGHOSTADDR", "203.0.113.9", ("203.0.113.9",)),
    ("PGSERVICE", "leak-service", ("leak-service",)),
    ("INSTAGRAM_APP_SECRET", "leak-secret-1", ("leak-secret",)),
    ("CLOUDINARY_API_SECRET", "leak-secret-2", ("leak-secret",)),
    ("TARGET_TELEGRAM_BOT_TOKEN", "leak-token-3", ("leak-token",)),
]


class TestEffectiveConfiguration:
    def test_it_is_a_no_op_until_the_gate_is_armed(self, monkeypatch):
        monkeypatch.setattr(guard, "ACTIVE", False)
        hostile = {"DB_HOST": "db.example.invalid", "INSTAGRAM_APP_SECRET": "x"}
        guard.validate_effective_configuration(hostile)

    def test_a_valid_environment_passes_once_armed(self, armed):
        assert refusal(GOOD) is None

    @pytest.mark.parametrize(
        "name, value, secrets",
        REFUSED,
        ids=[f"{n}-{i}" for i, (n, _, _) in enumerate(REFUSED)],
    )
    def test_each_rule_refuses_by_name_and_never_prints_the_value(
        self, armed, name, value, secrets
    ):
        message = refusal({**GOOD, name: value})
        assert message is not None
        assert named(message) == {name}
        for fragment in secrets:
            assert fragment not in message

    def test_a_missing_required_setting_is_refused_too(self, armed):
        env = {k: v for k, v in GOOD.items() if k not in ("DB_PORT", "ENCRYPTION_KEY")}
        assert named(refusal(env)) == {"DB_PORT", "ENCRYPTION_KEY"}

    def test_every_variable_the_example_file_names_is_covered(self, armed):
        """Derived from `.env.example` itself, with the allowlist written out
        here independently: a variable documented tomorrow is refused tomorrow."""
        text = (REPO / ".env.example").read_text(encoding="utf-8")
        listed = set(re.findall(r"^\s*#?\s*([A-Z][A-Z0-9_]*)=", text, re.M))
        kept = {"TEST_DB_NAME", "ENCRYPTION_KEY", "LOG_LEVEL", "WORKER_LOG_LEVEL"}
        kept |= {n for n in listed if n.startswith("DB_")}
        dsn = {"TARGET_DATABASE_URL", "DATABASE_URL"}
        assert {
            "INSTAGRAM_APP_SECRET",
            "CLOUDINARY_API_KEY",
            "ENCRYPTION_KEYS",
        } <= listed
        for name in sorted(listed - kept - dsn):
            message = refusal({**GOOD, name: "leak-sentinel"})
            assert message is not None and named(message) == {name}, name
            assert "leak-sentinel" not in message
        tolerated = {
            "LOG_LEVEL": "DEBUG",
            "WORKER_LOG_LEVEL": "DEBUG",
            "DB_SSLMODE": "disable",
        }
        assert refusal({**GOOD, **tolerated}) is None

    def test_a_variable_added_to_the_example_file_is_refused_without_an_edit_here(
        self, armed, monkeypatch, tmp_path
    ):
        example = tmp_path / ".env.example"
        example.write_text("# BRAND_NEW_PROVIDER_TOKEN=\nLOG_LEVEL=INFO\n")
        monkeypatch.setattr(guard, "ENV_EXAMPLE", example)
        assert named(refusal({**GOOD, "BRAND_NEW_PROVIDER_TOKEN": "x"})) == {
            "BRAND_NEW_PROVIDER_TOKEN"
        }
        assert refusal({**GOOD, "INSTAGRAM_APP_SECRET": "x"}) is None

    def test_an_unreadable_example_file_refuses_rather_than_guessing(
        self, armed, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(guard, "ENV_EXAMPLE", tmp_path / "missing")
        assert named(refusal()) == {".env.example"}

    def test_blank_values_count_as_unset(self, armed):
        assert (
            refusal({**GOOD, "INSTAGRAM_APP_SECRET": "", "CLOUDINARY_API_KEY": "  "})
            is None
        )

    @pytest.mark.parametrize(
        "url",
        [
            "postgresql://test_user:test_password@localhost:65433/storyline_test",
            "postgresql+asyncpg://test_user@[::1]:65433/storyline_test?sslmode=disable",
            "postgres://svc_worker:p%40ss@127.0.0.1:65433/runner_test_a_b",
        ],
    )
    def test_a_loopback_url_on_the_disposable_port_is_accepted(self, armed, url):
        assert (
            refusal({**GOOD, "TARGET_DATABASE_URL": url, "DATABASE_URL": url}) is None
        )

    def test_the_live_environment_is_the_default(self, armed, monkeypatch):
        monkeypatch.setenv("INSTAGRAM_APP_SECRET", "leak-secret")
        with pytest.raises(pytest.UsageError) as raised:
            guard.validate_effective_configuration()
        assert "INSTAGRAM_APP_SECRET" in str(raised.value)
        assert "leak-secret" not in str(raised.value)


# --- the plugin's early hook ------------------------------------------------------------


def early_config(flag, rootpath):
    """The part of pytest's Config that `pytest_load_initial_conftests` reads."""
    return SimpleNamespace(
        known_args_namespace=SimpleNamespace(isolated_provider_gate=flag),
        rootpath=rootpath,
    )


@pytest.fixture
def hook(monkeypatch, tmp_path):
    """The hook with `install` replaced by a recorder and ACTIVE restored."""
    installed = []
    monkeypatch.setattr(guard, "install", lambda: installed.append(True))
    monkeypatch.setattr(guard, "ACTIVE", False)
    monkeypatch.chdir(tmp_path)
    return installed


class TestEarlyHook:
    def test_without_the_flag_it_does_nothing_even_beside_a_dotenv(
        self, hook, tmp_path
    ):
        (tmp_path / ".env").write_text("DB_HOST=db.example.invalid\n")
        guard.pytest_load_initial_conftests(early_config(False, tmp_path))
        assert guard.ACTIVE is False and hook == []

    def test_a_dotenv_in_the_working_directory_is_refused(self, hook, tmp_path):
        (tmp_path / ".env").write_text("DB_HOST=db.example.invalid\n")
        with pytest.raises(pytest.UsageError) as raised:
            guard.pytest_load_initial_conftests(early_config(True, REPO))
        assert ".env: exists in the working directory" in str(raised.value)
        assert "db.example.invalid" not in str(raised.value)
        assert hook == []

    def test_a_guard_imported_from_another_checkout_is_refused(self, hook, tmp_path):
        elsewhere = tmp_path / "another-checkout"
        elsewhere.mkdir()
        with pytest.raises(pytest.UsageError) as raised:
            guard.pytest_load_initial_conftests(early_config(True, elsewhere))
        assert "tests.isolation_guard" in str(raised.value)
        assert hook == []

    def test_the_flag_arms_the_gate_before_anything_is_checked(self, hook, tmp_path):
        with pytest.raises(pytest.UsageError):
            guard.pytest_load_initial_conftests(early_config(True, tmp_path))
        assert guard.ACTIVE is True

    def test_one_dotenv_is_reported_once_when_the_cwd_is_the_rootdir(self, tmp_path):
        (tmp_path / ".env").write_text("DB_HOST=db.example.invalid\n")
        problems = guard._checkout_problems(tmp_path, tmp_path)
        dotenv = [why for name, why in problems if name == ".env"]
        assert len(dotenv) == 1 and "the working directory" in dotenv[0]


# --- the deny guard ---------------------------------------------------------------------


@pytest.fixture
def beneath(monkeypatch):
    """Recording stand-ins where the real entry points are, installed BEFORE the
    guard so it wraps them: what a call reaches is exactly what the guard let
    through, and nothing real is ever touched."""
    reached = []

    def stand_in(label, result):
        def fake(*args, **kwargs):
            reached.append((label, args, kwargs))
            return result

        return fake

    for name, result in (("connect", None), ("connect_ex", 0)):
        monkeypatch.setattr(socket.socket, name, stand_in(name, result))
    monkeypatch.setattr(
        socket, "create_connection", stand_in("create_connection", None)
    )
    monkeypatch.setattr(socket, "getaddrinfo", stand_in("getaddrinfo", []))
    monkeypatch.setattr(socket, "gethostbyname", stand_in("gethostbyname", "127.0.0.1"))
    monkeypatch.setattr(
        socket, "gethostbyname_ex", stand_in("gethostbyname_ex", ("h", [], []))
    )
    monkeypatch.setattr(
        psycopg2, "connect", stand_in("psycopg2.connect", "a connection")
    )
    monkeypatch.setattr(
        httpx.HTTPTransport, "handle_request", stand_in("http", httpx.Response(204))
    )

    async def async_fake(self, request):
        reached.append(("async http", (request.url,), {}))
        return httpx.Response(204)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", async_fake)
    monkeypatch.setattr(guard, "VIOLATIONS", [])
    return reached


@pytest.fixture
def guarded(beneath):
    guard.install()
    yield beneath
    guard.uninstall()


def connect_via(how, address):
    if how == "create_connection":
        return socket.create_connection(address)
    with socket.socket() as sock:
        return getattr(sock, how)(address)


CONNECTORS = ["connect", "connect_ex", "create_connection"]


class TestSockets:
    @pytest.mark.parametrize("how", CONNECTORS)
    @pytest.mark.parametrize(
        "address",
        [
            ("127.0.0.1", 65433),
            ("localhost", 65433),
            ("LOCALHOST", 65433),
            ("::1", 65433, 0, 0),
            (b"127.0.0.1", 65433),
        ],
    )
    def test_the_disposable_database_is_reachable(self, guarded, how, address):
        connect_via(how, address)
        assert [label for label, _, _ in guarded] == [how]
        assert guard.VIOLATIONS == []

    @pytest.mark.parametrize("how", CONNECTORS)
    @pytest.mark.parametrize(
        "address",
        [
            ("127.0.0.1", 5432),
            ("127.0.0.1", 80),
            ("203.0.113.9", 65433),
            ("graph.instagram.com", 443),
            ("0.0.0.0", 65433),
            ("127.0.0.1",),
            "/var/run/postgresql/.s.PGSQL.65433",
        ],
    )
    def test_everything_else_is_refused_before_the_real_call(
        self, guarded, how, address
    ):
        with pytest.raises(guard.IsolationViolation):
            connect_via(how, address)
        assert guarded == []
        assert len(guard.VIOLATIONS) == 1

    @pytest.mark.parametrize("how", CONNECTORS)
    def test_a_registered_fixture_port_is_reachable_on_loopback_only(
        self, guarded, how
    ):
        guard.allow_loopback(8081)
        connect_via(how, ("127.0.0.1", 8081))
        assert [label for label, _, _ in guarded] == [how]
        with pytest.raises(guard.IsolationViolation):
            connect_via(how, ("203.0.113.9", 8081))
        with pytest.raises(guard.IsolationViolation):
            connect_via(how, ("127.0.0.1", 8082))

    @pytest.mark.parametrize(
        "resolve", ["getaddrinfo", "gethostbyname", "gethostbyname_ex"]
    )
    def test_only_localhost_resolves(self, guarded, resolve):
        function = getattr(socket, resolve)
        function("localhost")
        function("127.0.0.1")
        with pytest.raises(guard.IsolationViolation):
            function("graph.instagram.com")
        assert [label for label, _, _ in guarded] == [resolve, resolve]

    def test_a_passive_lookup_has_no_name_to_leak(self, guarded):
        socket.getaddrinfo(None, 65433)
        socket.getaddrinfo("", 65433)
        assert len(guarded) == 2

    def test_a_refusal_is_not_an_exception_a_handler_can_swallow(self, guarded):
        swallowed = False
        with pytest.raises(guard.IsolationViolation) as raised:
            try:
                socket.create_connection(("graph.instagram.com", 443))
            except Exception:
                swallowed = True
        assert swallowed is False
        assert not isinstance(raised.value, Exception)

    def test_the_message_names_the_rule_and_never_the_destination(self, guarded):
        with pytest.raises(guard.IsolationViolation) as raised:
            socket.create_connection(("203.0.113.9", 4242))
        assert "203.0.113.9" not in str(raised.value)
        assert "4242" not in str(raised.value)
        assert "65433" in str(raised.value)


class TestLibpq:
    """libpq opens its own sockets, so the Python socket guard never sees a
    `psycopg2.connect`: its parameters are checked before it runs."""

    @pytest.mark.parametrize(
        "args, kwargs",
        [
            # the root conftest's own call shape
            (
                (),
                dict(
                    host="localhost",
                    port=65433,
                    user="test_user",
                    password="test_password",
                    database="postgres",
                ),
            ),
            (
                (),
                dict(
                    host="127.0.0.1",
                    port="65433",
                    dbname="storyline_test_0123456789",
                    user="test_user",
                ),
            ),
            (("postgresql://svc_worker:pw@localhost:65433/runner_test_ab_cd",), {}),
            (("host=::1 port=65433 dbname=storyline_ai user=svc_ingress",), {}),
            (
                ("postgresql://lineage_owner_abc@localhost/runner_tpl_x",),
                dict(port=65433),
            ),
        ],
    )
    def test_the_disposable_clusters_connections_pass_through(
        self, guarded, args, kwargs
    ):
        assert psycopg2.connect(*args, **kwargs) == "a connection"
        assert len(guarded) == 1 and guard.VIOLATIONS == []

    def test_the_callers_arguments_are_forwarded_unchanged(self, guarded):
        factory = object()
        dsn = "postgresql://svc_worker:pw@localhost:65433/runner_test_ab_cd"
        psycopg2.connect(dsn, None, factory, async_=1)
        ((_, args, kwargs),) = guarded
        assert args == (dsn, None, factory) and kwargs == {"async_": 1}

    @pytest.mark.parametrize(
        "args, kwargs, parameter",
        [
            (
                (),
                dict(
                    host="db.example.invalid",
                    port=65433,
                    user="test_user",
                    database="postgres",
                ),
                "host",
            ),
            ((), dict(port=65433, user="test_user", database="postgres"), "host"),
            (
                (),
                dict(
                    host="localhost,db.example.invalid",
                    port=65433,
                    user="test_user",
                    database="postgres",
                ),
                "host",
            ),
            (
                (),
                dict(
                    host="localhost",
                    hostaddr="203.0.113.9",
                    port=65433,
                    user="test_user",
                    database="postgres",
                ),
                "hostaddr",
            ),
            (
                (),
                dict(
                    host="localhost", port=5432, user="test_user", database="postgres"
                ),
                "port",
            ),
            ((), dict(host="localhost", user="test_user", database="postgres"), "port"),
            (
                (),
                dict(
                    host="localhost",
                    port="65433,5432",
                    user="test_user",
                    database="postgres",
                ),
                "port",
            ),
            (
                (),
                dict(
                    host="localhost", port=65433, user="test_user", database="storydump"
                ),
                "dbname",
            ),
            ((), dict(host="localhost", port=65433, user="test_user"), "dbname"),
            (
                (),
                dict(
                    host="localhost",
                    port=65433,
                    user="storydump_user",
                    database="postgres",
                ),
                "user",
            ),
            ((), dict(host="localhost", port=65433, database="postgres"), "user"),
            ((), dict(service="production"), "service"),
            (
                (
                    "postgresql://test_user@localhost:65433/postgres?host=db.example.invalid",
                ),
                {},
                "host",
            ),
            (
                (
                    "postgresql://test_user@localhost:65433/postgres?hostaddr=203.0.113.9",
                ),
                {},
                "hostaddr",
            ),
            ((), {}, "host"),
        ],
    )
    def test_anything_else_is_refused_before_libpq_runs(
        self, guarded, args, kwargs, parameter
    ):
        with pytest.raises(guard.IsolationViolation) as raised:
            psycopg2.connect(*args, **kwargs)
        assert guarded == []
        assert f"its {parameter} is not" in str(raised.value)
        for value in ("db.example.invalid", "203.0.113.9", "storydump_user"):
            assert value not in str(raised.value)


class TestHttpx:
    def test_a_real_transport_is_refused_before_any_connection(self, guarded):
        with httpx.Client(transport=httpx.HTTPTransport()) as client:
            with pytest.raises(guard.IsolationViolation):
                client.get("https://graph.instagram.com/me")
        assert guarded == []

    def test_a_real_async_transport_is_refused_too(self, guarded):
        async def attempt():
            async with httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport()
            ) as client:
                await client.get("https://graph.instagram.com/me")

        with pytest.raises(guard.IsolationViolation):
            asyncio.run(attempt())
        assert guarded == []

    def test_an_in_process_transport_still_works(self, guarded):
        mock = httpx.MockTransport(
            lambda request: httpx.Response(200, json={"ok": True})
        )
        with httpx.Client(transport=mock) as client:
            assert client.get("https://graph.instagram.com/me").json() == {"ok": True}
        assert guarded == [] and guard.VIOLATIONS == []

    def test_a_registered_loopback_fixture_is_reachable_and_nothing_else_is(
        self, guarded
    ):
        guard.allow_loopback(8081)
        with httpx.Client(transport=httpx.HTTPTransport()) as client:
            assert client.get("http://127.0.0.1:8081/x").status_code == 204
            for url in ("http://127.0.0.1:8082/x", "http://203.0.113.9:8081/x"):
                with pytest.raises(guard.IsolationViolation):
                    client.get(url)
        assert [label for label, _, _ in guarded] == ["http"]


class TestRealTraffic:
    """The allowed path with nothing stubbed: real loopback sockets, reached the
    ways the gate will reach them (a plain connect, the event loop by name as
    asyncpg does, a real httpx client). It binds a loopback port, so like
    `test_integration_coverage_policy.py` it fails inside a sandbox without
    network access: that is the sandbox, not the guard."""

    #: A loopback port nothing registered. The guard refuses it before any
    #: connect, so it never needs to be free.
    UNREGISTERED = 8082

    @pytest.fixture
    def installed(self, monkeypatch):
        monkeypatch.setattr(guard, "VIOLATIONS", [])
        guard.install()
        yield
        guard.uninstall()

    def test_a_plain_connect_reaches_a_registered_server_and_nothing_else(
        self, installed
    ):
        with socket.create_server(("127.0.0.1", 0)) as server:
            port = server.getsockname()[1]
            guard.allow_loopback(port)
            socket.create_connection(("localhost", port)).close()
            for host, other in (("localhost", self.UNREGISTERED), ("db.invalid", port)):
                with pytest.raises(guard.IsolationViolation):
                    socket.create_connection((host, other))

    async def test_the_event_loop_reaches_a_registered_server_by_name(self, installed):
        async def echo(reader, writer):
            writer.write(await reader.readline())
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(echo, "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            guard.allow_loopback(port)
            reader, writer = await asyncio.open_connection("localhost", port)
            writer.write(b"ping\n")
            assert await reader.readline() == b"ping\n"
            writer.close()
            await writer.wait_closed()
            for host, other in (("localhost", self.UNREGISTERED), ("db.invalid", port)):
                with pytest.raises(guard.IsolationViolation):
                    await asyncio.open_connection(host, other)
        finally:
            server.close()
            await server.wait_closed()

    async def test_a_real_httpx_client_reaches_a_registered_fixture_server(
        self, installed
    ):
        async def respond(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n")
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(respond, "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            guard.allow_loopback(port)
            async with httpx.AsyncClient(trust_env=False) as client:
                assert (
                    await client.get(f"http://127.0.0.1:{port}/")
                ).status_code == 204
                for url in (
                    f"http://127.0.0.1:{self.UNREGISTERED}/",
                    "https://graph.instagram.com/me",
                ):
                    with pytest.raises(guard.IsolationViolation):
                        await client.get(url)
        finally:
            server.close()
            await server.wait_closed()


class TestInstall:
    def test_uninstall_restores_every_entry_point_exactly(self, beneath):
        points = [
            (socket.socket, "connect"),
            (socket.socket, "connect_ex"),
            (socket, "create_connection"),
            (socket, "getaddrinfo"),
            (socket, "gethostbyname"),
            (socket, "gethostbyname_ex"),
            (psycopg2, "connect"),
            (httpx.HTTPTransport, "handle_request"),
            (httpx.AsyncHTTPTransport, "handle_async_request"),
        ]
        before = [getattr(owner, name) for owner, name in points]
        meta_path = list(sys.meta_path)
        guard.install()
        try:
            assert all(getattr(o, n) is not b for (o, n), b in zip(points, before))
        finally:
            guard.uninstall()
        assert [getattr(owner, name) for owner, name in points] == before
        assert sys.meta_path == meta_path

    def test_installing_twice_wraps_once(self, beneath):
        guard.install()
        try:
            once = socket.create_connection
            guard.install()
            assert socket.create_connection is once
        finally:
            guard.uninstall()

    def test_uninstall_forgets_registered_fixture_ports(self, beneath):
        guard.install()
        guard.allow_loopback(8081)
        guard.uninstall()
        guard.install()
        try:
            with pytest.raises(guard.IsolationViolation):
                socket.create_connection(("127.0.0.1", 8081))
        finally:
            guard.uninstall()


class TestTheLedger:
    def test_a_refusal_that_a_handler_swallowed_still_fails_the_session(
        self, guarded, monkeypatch
    ):
        monkeypatch.setattr(guard, "ACTIVE", True)
        try:
            socket.create_connection(("graph.instagram.com", 443))
        except BaseException:  # the worst case: a handler that swallows even this
            pass
        session = SimpleNamespace(exitstatus=pytest.ExitCode.OK)
        guard.pytest_sessionfinish(session, 0)
        assert session.exitstatus == pytest.ExitCode.TESTS_FAILED

    def test_a_clean_session_keeps_its_status(self, guarded, monkeypatch):
        monkeypatch.setattr(guard, "ACTIVE", True)
        session = SimpleNamespace(exitstatus=pytest.ExitCode.OK)
        guard.pytest_sessionfinish(session, 0)
        assert session.exitstatus == pytest.ExitCode.OK

    def test_a_session_that_never_armed_the_gate_is_left_alone(self, guarded):
        session = SimpleNamespace(exitstatus=pytest.ExitCode.OK)
        guard.VIOLATIONS.append("recorded")
        guard.pytest_sessionfinish(session, 0)
        assert session.exitstatus == pytest.ExitCode.OK


# --- the import hooks -------------------------------------------------------------------


class TestImportHooks:
    def test_a_denied_module_is_refused_without_running_it(
        self, guarded, monkeypatch, tmp_path
    ):
        (tmp_path / "isogate_denied.py").write_text("raise AssertionError('it ran')\n")
        monkeypatch.syspath_prepend(tmp_path)
        monkeypatch.setitem(guard.DENIED_IMPORTS, "isogate_denied", "a test reason")
        with pytest.raises(guard.IsolationViolation, match="isogate_denied"):
            importlib.import_module("isogate_denied")
        assert "isogate_denied" not in sys.modules

    def test_a_patched_module_loads_normally_and_then_loses_its_entrypoints(
        self, guarded, monkeypatch, tmp_path
    ):
        (tmp_path / "isogate_worker.py").write_text(
            "def main():\n    return 'ran'\n\n\ndef run(app):\n    return 'ran'\n\n\n"
            "def compose():\n    return 'composed'\n"
        )
        monkeypatch.syspath_prepend(tmp_path)
        monkeypatch.setitem(
            guard.PATCHED_AFTER_IMPORT,
            "isogate_worker",
            guard._forbid_worker_entrypoints,
        )
        try:
            module = importlib.import_module("isogate_worker")
            assert module.compose() == "composed"
            # whatever else a tool asks the loader still reaches the real one
            source = module.__loader__.get_source("isogate_worker")
            assert "def compose" in source
            for entry in ("main", "run"):
                with pytest.raises(guard.IsolationViolation, match=entry):
                    getattr(module, entry)()
        finally:
            sys.modules.pop("isogate_worker", None)

    def test_a_module_the_hooks_do_not_name_is_untouched(
        self, guarded, monkeypatch, tmp_path
    ):
        (tmp_path / "isogate_other.py").write_text("VALUE = 7\n")
        monkeypatch.syspath_prepend(tmp_path)
        try:
            assert importlib.import_module("isogate_other").VALUE == 7
        finally:
            sys.modules.pop("isogate_other", None)

    def test_the_real_entrypoints_and_provider_clients_are_refused(self, tmp_path):
        """Fresh interpreter: `src` must not be imported before the guard is."""
        code = textwrap.dedent(
            """
            import importlib
            from tests import isolation_guard as guard

            guard.install()
            for name in ("src.main", "cloudinary.uploader", "cloudinary.api"):
                try:
                    importlib.import_module(name)
                except guard.IsolationViolation:
                    print("refused", name)
                else:
                    raise SystemExit(name + " was imported")
            import cloudinary.utils  # signing helpers stay importable
            import src.worker as worker  # composition stays importable

            assert callable(worker.compose)
            for entry in ("main", "run"):
                try:
                    getattr(worker, entry)()
                except guard.IsolationViolation:
                    print("refused", "src.worker." + entry)
                else:
                    raise SystemExit("src.worker." + entry + " ran")
            """
        )
        done = subprocess.run(
            [sys.executable, "-c", code],
            cwd=tmp_path,
            env=clean_env(),
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert done.returncode == 0, done.stdout + done.stderr
        assert done.stdout.splitlines() == [
            "refused src.main",
            "refused cloudinary.uploader",
            "refused cloudinary.api",
            "refused src.worker.main",
            "refused src.worker.run",
        ]


# --- the plugin in a real pytest run, with the spy beneath it ---------------------------


class Run:
    def __init__(self, completed, spy):
        self.code = completed.returncode
        self.output = completed.stdout + completed.stderr
        self.spy = spy


@pytest.fixture
def pytest_run(tmp_path):
    """Launch pytest the way the gate's recipe does: from a working directory of
    its own (holding `.env.test`), with a clean environment, `PYTHONPATH` naming
    the tree under test, and the spy loaded first."""
    rootdir = tmp_path / "rootdir"
    rootdir.mkdir()
    (rootdir / "tests").symlink_to(REPO / "tests", target_is_directory=True)
    (rootdir / "pytest.ini").symlink_to(REPO / "pytest.ini")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    log = tmp_path / "spy.log"

    def run(
        env=None,
        *,
        dotenv_test=None,
        dotenv=None,
        rootdir_dotenv=None,
        plugin=True,
        flag=True,
        collect_only=False,
        another_checkout=False,
        control=False,
        target=None,
    ):
        for directory, name, text in (
            (cwd, ".env.test", dotenv_test),
            (cwd, ".env", dotenv),
            (rootdir, ".env", rootdir_dotenv),
        ):
            if text is not None:
                (directory / name).write_text(text)
        command = [sys.executable, "-m", "pytest", "-p", "tests._isolation_spy"]
        command += ["-p", "tests.isolation_guard"] if plugin else []
        command += [guard.OPTION] if flag else []
        command += ["-c", str(rootdir / "pytest.ini"), "--rootdir", str(rootdir)]
        command += ["-p", "no:cacheprovider", "--no-cov", "-q"]
        command += ["--collect-only"] if collect_only else []
        command.append(
            target or str(rootdir / (CONTROL_TARGET if control else GATE_TARGET))
        )
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=clean_env(
                PYTHONPATH=os.pathsep.join(
                    [str(REPO)] if another_checkout else [str(rootdir), str(REPO)]
                ),
                ISOLATION_SPY_LOG=str(log),
                **(GOOD if env is None else env),
            ),
            capture_output=True,
            text=True,
            timeout=180,
        )
        return Run(completed, log.read_text().splitlines() if log.exists() else [])

    return run


#: Each case is refused before anything connects: exit 4 (a usage error), the
#: variable (or file, or module) named in the refusal's own `  NAME: reason`
#: format, the value absent from the output, and an empty spy log.
#: (a label, run arguments, the name that must be printed, values that must not be)
REFUSED_RUNS = [
    (
        "dotenv-test-overrides-the-shell",
        dict(dotenv_test="DB_HOST=db.example.invalid\n"),
        "DB_HOST",
        ("db.example.invalid",),
    ),
    (
        "inherited-provider-secret",
        dict(env={**GOOD, "INSTAGRAM_APP_SECRET": "leak-secret-1"}),
        "INSTAGRAM_APP_SECRET",
        ("leak-secret-1",),
    ),
    (
        "non-loopback-dsn",
        dict(env={**GOOD, "TARGET_DATABASE_URL": LEAK_URL}),
        "TARGET_DATABASE_URL",
        LEAK_PIECES,
    ),
    (
        "dotenv-in-the-working-directory",
        dict(dotenv="DB_HOST=db.example.invalid\n"),
        ".env",
        ("db.example.invalid",),
    ),
    (
        "dotenv-in-the-rootdir",
        dict(rootdir_dotenv="DB_HOST=db.example.invalid\n"),
        ".env",
        ("db.example.invalid",),
    ),
    ("wrong-port", dict(env={**GOOD, "DB_PORT": "5432"}), "DB_PORT", ("5432",)),
    (
        "guard-imported-from-another-checkout",
        dict(another_checkout=True),
        "tests.isolation_guard",
        (),
    ),
    (
        "rotation-keys",
        dict(env={**GOOD, "ENCRYPTION_KEYS": "leak-key-1,leak-key-2"}),
        "ENCRYPTION_KEYS",
        ("leak-key-1", "leak-key-2"),
    ),
]


class TestThePluginInARealRun:
    @pytest.mark.parametrize(
        "arguments, name, secrets",
        [case[1:] for case in REFUSED_RUNS],
        ids=[case[0] for case in REFUSED_RUNS],
    )
    def test_a_bad_configuration_is_refused_before_anything_connects(
        self, pytest_run, arguments, name, secrets
    ):
        run = pytest_run(**arguments)
        assert run.code == pytest.ExitCode.USAGE_ERROR, run.output
        assert f"  {name}: " in run.output
        for secret in secrets:
            assert secret not in run.output
        assert run.spy == []

    def test_a_good_configuration_collects_and_nothing_external_is_attempted(
        self, pytest_run
    ):
        run = pytest_run(collect_only=True)
        assert run.code == pytest.ExitCode.OK, run.output
        assert Path(GATE_TARGET).name in run.output
        assert run.spy == []

    def test_the_root_fixtures_database_probe_is_permitted_through_to_the_spy(
        self, pytest_run
    ):
        """The positive control for everything above: armed, with a valid
        configuration, the first thing the root autouse fixture does is probe
        the disposable port, and the guard lets exactly that through."""
        run = pytest_run(GOOD_WITHOUT_A_DATABASE, control=True)
        assert run.code == pytest.ExitCode.OK, run.output
        assert run.spy == ["psycopg2.connect", "socket.create_connection"]

    @pytest.mark.parametrize(
        "plugin, flag",
        [(False, False), (True, False)],
        ids=["no-plugin", "plugin-without-the-flag"],
    )
    def test_without_the_flag_a_hostile_run_is_untouched_and_reaches_the_spy(
        self, pytest_run, plugin, flag
    ):
        """The control that gives every empty spy log above its meaning, and the
        proof that the conftest hook changes nothing for a normal run: the same
        hostile `.env.test`, unrefused, goes straight to the database probe."""
        run = pytest_run(
            {"INSTAGRAM_APP_SECRET": "leak-secret-1"},
            dotenv_test="DB_HOST=db.example.invalid\n",
            plugin=plugin,
            flag=flag,
            control=True,
        )
        assert run.code == pytest.ExitCode.OK, run.output
        assert run.spy == ["psycopg2.connect", "socket.create_connection"]

    def test_the_flag_without_the_plugin_is_pytests_own_unrecognized_argument(
        self, pytest_run
    ):
        run = pytest_run(plugin=False)
        assert run.code == pytest.ExitCode.USAGE_ERROR
        assert "unrecognized arguments: --isolated-provider-gate" in run.output
        assert run.spy == []

    def test_the_plugin_installs_the_deny_guard_and_a_swallowed_refusal_fails_the_run(
        self, pytest_run, tmp_path
    ):
        """A test that swallows even `IsolationViolation` passes by itself and
        the run still fails: the ledger, not the exception, is the last word.
        The file lies outside the rootdir, so no conftest loads and the guard
        under test is the one the plugin installed."""
        swallow = tmp_path / "elsewhere" / "test_swallow.py"
        swallow.parent.mkdir()
        swallow.write_text(
            textwrap.dedent(
                """
                import socket


                def test_the_test_itself_passes():
                    try:
                        socket.create_connection(("graph.instagram.com", 443))
                    except BaseException:
                        pass
                """
            )
        )
        run = pytest_run(target=str(swallow))
        assert "1 passed" in run.output
        assert "refused by the isolated provider gate" in run.output
        assert run.code == pytest.ExitCode.TESTS_FAILED
        assert run.spy == []


class TestANormalRunIsUnaffected:
    def test_importing_the_module_changes_nothing(self, tmp_path):
        """What `tests/conftest.py` does in every ordinary run."""
        code = textwrap.dedent(
            """
            import socket, sys
            import httpx, psycopg2

            def state():
                return (
                    socket.socket.connect, socket.socket.connect_ex,
                    socket.create_connection, socket.getaddrinfo,
                    psycopg2.connect, httpx.HTTPTransport.handle_request,
                    httpx.AsyncHTTPTransport.handle_async_request,
                    tuple(sys.meta_path),
                )

            before = state()
            from tests import isolation_guard as guard
            assert state() == before
            assert guard.ACTIVE is False
            guard.validate_effective_configuration({"DB_HOST": "db.example.invalid"})
            print("unchanged")
            """
        )
        done = subprocess.run(
            [sys.executable, "-c", code],
            cwd=tmp_path,
            env=clean_env(),
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert done.returncode == 0, done.stdout + done.stderr
        assert done.stdout.strip() == "unchanged"
