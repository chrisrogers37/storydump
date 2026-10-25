"""Tests for logger utility, and for what a log line may print of a request."""

import asyncio
import io
import json
import logging
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from src.utils.logger import setup_logger

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.unit
class TestLogger:
    """Test suite for logger utility."""

    def test_setup_logger_creates_logger(self):
        """Test that setup_logger creates a logger instance."""
        logger = setup_logger("test_logger")

        assert isinstance(logger, logging.Logger)
        assert logger.name == "test_logger"

    def test_setup_logger_with_custom_level(self):
        """Test setting custom log level."""
        logger = setup_logger("test_custom_level", level=logging.WARNING)

        assert logger.level == logging.WARNING

    def test_setup_logger_creates_log_directory(self):
        """Test that setup_logger creates log directory if it doesn't exist."""
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "test_logs" / "test.log"

            setup_logger("test_dir", log_file=str(log_file))

            assert log_file.parent.exists()

    def test_logger_writes_to_file(self):
        """Test that logger writes messages to file."""
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "test.log"

            logger = setup_logger(
                "file_logger", level=logging.INFO, log_file=str(log_file)
            )

            test_message = "Test log message"
            logger.info(test_message)

            # Read log file
            assert log_file.exists()
            with open(log_file, "r") as f:
                content = f.read()
                assert test_message in content

    def test_logger_respects_level(self):
        """Test that logger respects log level filtering."""
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "level_test.log"

            logger = setup_logger(
                "level_logger", level=logging.WARNING, log_file=str(log_file)
            )

            # Log at different levels
            logger.debug("Debug message")
            logger.info("Info message")
            logger.warning("Warning message")
            logger.error("Error message")

            # Read log file
            with open(log_file, "r") as f:
                content = f.read()

                # DEBUG and INFO should not be logged
                assert "Debug message" not in content
                assert "Info message" not in content

                # WARNING and ERROR should be logged
                assert "Warning message" in content
                assert "Error message" in content

    def test_logger_includes_timestamp(self):
        """Test that log messages include timestamp."""
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "timestamp_test.log"

            logger = setup_logger("timestamp_logger", log_file=str(log_file))

            logger.info("Timestamped message")

            # Read log file
            with open(log_file, "r") as f:
                content = f.read()

                # Check for timestamp pattern (YYYY-MM-DD HH:MM:SS)
                import re

                timestamp_pattern = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}"
                assert re.search(timestamp_pattern, content) is not None

    def test_logger_includes_level_name(self):
        """Test that log messages include level name."""
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "level_name_test.log"

            logger = setup_logger("level_name_logger", log_file=str(log_file))

            logger.warning("Test warning")

            # Read log file
            with open(log_file, "r") as f:
                content = f.read()
                assert "WARNING" in content


#: Obviously fake values: a test that finds one of them in a log line fails.
FAKE_TOKEN = "fake-invite-token-0000"
FAKE_CODE = "fake-code-0000"
FAKE_STATE = "fake-state-0000"
FAKE_CAPABILITY = "fake-capability-0000"
FAKES = (FAKE_TOKEN, FAKE_CODE, FAKE_STATE, FAKE_CAPABILITY)
WS = "00000000-0000-0000-0000-000000000001"


@pytest.mark.unit
class TestWhatALogLinePrintsOfARequest:
    @pytest.mark.parametrize(
        "target, printed",
        [
            ("/health", "/health"),
            (
                f"/auth/google/callback?code={FAKE_CODE}&state={FAKE_STATE}",
                "/auth/google/callback?<redacted>",
            ),
            (
                f"/webhooks/meta/data-deletion/status?code={FAKE_CAPABILITY}",
                "/webhooks/meta/data-deletion/status?<redacted>",
            ),
            ("/auth/signout?everywhere=true", "/auth/signout?<redacted>"),
            ("/api/v1/me?", "/api/v1/me?<redacted>"),
            # Everything after a credential prefix prints as one <redacted>,
            # whatever follows it: the token, and any segment after it.
            (
                f"/api/v1/invitations/{FAKE_TOKEN}/accept",
                "/api/v1/invitations/<redacted>",
            ),
            (
                f"/api/v1/invitations/{FAKE_TOKEN}/accept/",
                "/api/v1/invitations/<redacted>",
            ),
            (f"/api/v1/invitations/{FAKE_TOKEN}", "/api/v1/invitations/<redacted>"),
            (
                f"/api/v1/invitations/{FAKE_TOKEN}/accept?code={FAKE_CODE}",
                "/api/v1/invitations/<redacted>?<redacted>",
            ),
            ("/api/v1/invitations/", "/api/v1/invitations/"),
            (
                f"/api/v1/workspaces/{WS}/intents?limit=5",
                f"/api/v1/workspaces/{WS}/intents?<redacted>",
            ),
            (
                f"/api/v1/me/invitations/{WS}/resend",
                f"/api/v1/me/invitations/{WS}/resend",
            ),
            (
                f"/api/v1/workspaces/{WS}/invitations",
                f"/api/v1/workspaces/{WS}/invitations",
            ),
            # A token segment of any shape: one that is empty once its
            # parameters are set aside (`;<T>`, printed `%3B<T>`, and `.;<T>`;
            # Starlette routes each of them to the accept handler), a dot-dot,
            # or a segment before it.
            (
                f"/api/v1/invitations/;{FAKE_TOKEN}/accept",
                "/api/v1/invitations/<redacted>",
            ),
            (
                f"/api/v1/invitations/%3B{FAKE_TOKEN}/accept",
                "/api/v1/invitations/<redacted>",
            ),
            (
                f"/api/v1/invitations/.;{FAKE_TOKEN}/accept",
                "/api/v1/invitations/<redacted>",
            ),
            (f"/api/v1/invitations/;{FAKE_TOKEN}", "/api/v1/invitations/<redacted>"),
            (f"/join/;{FAKE_TOKEN}", "/join/<redacted>"),
            (
                f"/api/v1/invitations/../{FAKE_TOKEN}/accept",
                "/api/v1/invitations/<redacted>",
            ),
            (f"/api/v1/invitations/x/{FAKE_TOKEN}", "/api/v1/invitations/<redacted>"),
            (
                f"/api/v1/invitations/%09/{FAKE_TOKEN}",
                "/api/v1/invitations/<redacted>",
            ),
            # The prefix anywhere in the path, not only at its start: uvicorn's
            # --root-path, a base URL with a trailing slash, an absolute-form
            # target (the access line prints `http%3A//...`), empty or `.`
            # segments inside it, a segment's parameters (`;p=1`, printed
            # `%3Bp%3D1`) and any letter case. Most of these 404, and uvicorn
            # still logs them; under a configured root path the request routes.
            (
                f"/x/api/v1/invitations/{FAKE_TOKEN}/accept",
                "/x/api/v1/invitations/<redacted>",
            ),
            (
                f"//api/v1/invitations/{FAKE_TOKEN}/accept",
                "//api/v1/invitations/<redacted>",
            ),
            (
                f"/api//v1/invitations/{FAKE_TOKEN}/accept",
                "/api//v1/invitations/<redacted>",
            ),
            (
                f"/api/v1/./invitations/{FAKE_TOKEN}/accept",
                "/api/v1/./invitations/<redacted>",
            ),
            (
                f"/api/v1/invitations//{FAKE_TOKEN}/accept",
                "/api/v1/invitations/<redacted>",
            ),
            (
                f"/api/v1/invitations;p=1/{FAKE_TOKEN}/accept",
                "/api/v1/invitations/<redacted>",
            ),
            (
                f"/api/v1/invitations%3Bp%3D1/{FAKE_TOKEN}/accept",
                "/api/v1/invitations/<redacted>",
            ),
            (
                f"/API/V1/INVITATIONS/{FAKE_TOKEN}/accept",
                "/API/V1/INVITATIONS/<redacted>",
            ),
            (
                f"http://api.example.test/api/v1/invitations/{FAKE_TOKEN}/accept",
                "http://api.example.test/api/v1/invitations/<redacted>",
            ),
            (
                f"http%3A//api.example.test/api/v1/invitations/{FAKE_TOKEN}/accept"
                f"?code={FAKE_CODE}",
                "http%3A//api.example.test/api/v1/invitations/<redacted>?<redacted>",
            ),
            # The site's join link, which the API serves no route for: a hit is
            # a misdirected invitation link.
            (f"/join/{FAKE_TOKEN}", "/join/<redacted>"),
            (f"/join/{FAKE_TOKEN}/start", "/join/<redacted>"),
            # The first prefix in the path decides: what follows it is masked
            # whole, a later prefix included, whichever prefix comes first.
            (f"/join/{FAKE_TOKEN}/api/v1/invitations/x", "/join/<redacted>"),
            (
                f"/api/v1/invitations/{FAKE_TOKEN}/api/v1/invitations/x",
                "/api/v1/invitations/<redacted>",
            ),
        ],
    )
    def test_a_target_prints_without_its_query_or_its_credential(self, target, printed):
        from src.utils.logger import redact_target

        assert redact_target(target) == printed
        assert redact_target(printed) == printed, "redacting twice changes nothing"

    def test_a_routed_request_keeps_its_classified_values_and_hides_the_rest(self):
        from src.utils.logger import loggable_path

        command = SimpleNamespace(path="/api/v1/workspaces/{ws}/commands/{command}")
        scope = {"route": command, "path_params": {"ws": WS, "command": "approve"}}
        assert loggable_path(scope) == f"/api/v1/workspaces/{WS}/commands/approve"
        accept = SimpleNamespace(path="/api/v1/invitations/{token}/accept")
        scope = {
            "route": accept,
            "path_params": {"token": FAKE_TOKEN},
            "path": f"/api/v1/invitations/{FAKE_TOKEN}/accept",
        }
        assert loggable_path(scope) == "/api/v1/invitations/{token}/accept"
        unclassified = SimpleNamespace(path="/x/{mystery}")
        scope = {"route": unclassified, "path_params": {"mystery": "value"}}
        assert loggable_path(scope) == "/x/{mystery}"

    def test_a_printed_value_cannot_start_a_line_or_fake_a_segment(self):
        from src.utils.logger import loggable_path

        command = SimpleNamespace(path="/api/v1/workspaces/{ws}/commands/{command}")
        forged = {"ws": WS, "command": "approve\nINFO forged/line"}
        printed = loggable_path({"route": command, "path_params": forged})
        assert printed == (
            f"/api/v1/workspaces/{WS}/commands/approve%0AINFO%20forged%2Fline"
        )
        assert loggable_path({"path": "/x\nforged"}) == "/x%0Aforged"

    def test_an_unrouted_request_is_named_by_its_masked_path(self):
        from src.utils.logger import loggable_path

        unrouted = {"path": f"/api/v1/invitations/{FAKE_TOKEN}/accept"}
        assert loggable_path(unrouted) == "/api/v1/invitations/<redacted>"
        doubled = {"path": f"//api/v1/invitations/{FAKE_TOKEN}/accept"}
        assert loggable_path(doubled) == "//api/v1/invitations/<redacted>"
        # Percent-encoded first (`%3B`), as the access line prints it.
        parameter = {"path": f"/api/v1/invitations/;{FAKE_TOKEN}/accept"}
        assert loggable_path(parameter) == "/api/v1/invitations/<redacted>"
        assert loggable_path({"path": "/health"}) == "/health"
        assert loggable_path({}) == ""


def _uvicorn_access_record(target: str, status: int) -> logging.LogRecord:
    """An access record as uvicorn 0.47.0 makes one
    (`uvicorn/protocols/http/h11_impl.py:481-488`)."""
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("10.0.0.7:41000", "GET", target, "1.1", status),
        None,
    )


def _access_formatter():
    """uvicorn's own access formatter, as its `LOGGING_CONFIG` builds it."""
    from uvicorn.config import LOGGING_CONFIG
    from uvicorn.logging import AccessFormatter

    fmt = LOGGING_CONFIG["formatters"]["access"]["fmt"]
    return AccessFormatter(fmt=fmt, use_colors=False)


@pytest.mark.unit
class TestTheAccessLogRedactor:
    def test_the_import_installs_exactly_one_on_each_logger(self):
        from src.utils.logger import (
            REDACTED_LOGGERS,
            AccessLogRedactor,
            install_access_log_redaction,
        )

        def installed(name):
            filters = logging.getLogger(name).filters
            return [type(f) for f in filters].count(AccessLogRedactor)

        assert REDACTED_LOGGERS == ("uvicorn.access", "uvicorn.asgi")
        # Before any call here: what this process's import installed. An
        # earlier test can call it too, so `TestEachProcessIsConfiguredAtImport`
        # is what proves the import alone does, in a fresh interpreter.
        assert [installed(name) for name in REDACTED_LOGGERS] == [1, 1]
        install_access_log_redaction()  # a second call adds nothing
        assert [installed(name) for name in REDACTED_LOGGERS] == [1, 1]

    def test_a_trace_scope_loses_its_target_on_a_copy(self):
        from src.utils.logger import AccessLogRedactor

        target = f"/api/v1/invitations/{FAKE_TOKEN}/accept"
        scope = {
            "type": "http",
            "path": target,
            "raw_path": target.encode("ascii"),
            "query_string": f"code={FAKE_CODE}".encode("ascii"),
            "headers": "<...>",
        }
        record = logging.LogRecord(
            "uvicorn.asgi",
            5,  # uvicorn's TRACE
            __file__,
            0,
            "%s [%d] Started scope=%s",
            ("10.0.0.7:41000 - ASGI", 1, scope),
            None,
        )
        assert AccessLogRedactor().filter(record) is True
        line = record.getMessage()
        assert "'path': '/api/v1/invitations/<redacted>'" in line
        assert "'raw_path': '<redacted>'" in line
        assert "'query_string': '<redacted>'" in line
        assert [fake for fake in FAKES if fake in line] == []
        assert scope["path"] == target, "the scope itself is untouched"

    def test_a_uvicorn_record_keeps_its_shape_and_loses_its_query(self):
        from src.utils.logger import AccessLogRedactor

        record = _uvicorn_access_record(
            f"/auth/google/callback?code={FAKE_CODE}&state={FAKE_STATE}", 302
        )
        assert AccessLogRedactor().filter(record) is True
        assert record.args == (
            "10.0.0.7:41000",
            "GET",
            "/auth/google/callback?<redacted>",
            "1.1",
            302,
        )
        line = _access_formatter().format(record)
        assert '"GET /auth/google/callback?<redacted> HTTP/1.1" 302 Found' in line
        assert [fake for fake in FAKES if fake in line] == []

    def test_a_record_of_another_shape_passes_untouched(self):
        from src.utils.logger import AccessLogRedactor

        record = logging.LogRecord(
            "uvicorn.access", logging.INFO, __file__, 0, "no arguments", None, None
        )
        assert AccessLogRedactor().filter(record) is True
        assert record.args is None
        assert record.getMessage() == "no arguments"


async def _answer(scope, receive, send):
    """The smallest ASGI app: 200, no body."""
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-length", b"0")],
        }
    )
    await send({"type": "http.response.body", "body": b""})


class _FakeTransport:
    """What uvicorn's `H11Protocol` asks of a transport, with no socket."""

    def __init__(self):
        self.written = b""
        self.closed = False

    def get_extra_info(self, name, default=None):
        known = {"peername": ("10.0.0.7", 41000), "sockname": ("127.0.0.1", 8000)}
        return known.get(name, default)

    def write(self, data):
        self.written += data

    def close(self):
        self.closed = True

    def is_closing(self):
        return self.closed

    def pause_reading(self):
        pass

    def resume_reading(self):
        pass


@pytest.fixture
def access_lines():
    """uvicorn's access logger at INFO with uvicorn's own formatter on a buffer,
    restored afterwards. The fixture adds no redactor: the one on the logger is
    whatever this process installed. That the import alone installs it is
    `TestEachProcessIsConfiguredAtImport`'s to prove, in a fresh interpreter."""
    import src.utils.logger  # noqa: F401 — its import installs the redactor

    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(_access_formatter())
    access = logging.getLogger("uvicorn.access")
    previous = access.level
    access.setLevel(logging.INFO)
    access.addHandler(handler)
    try:
        yield lambda: buffer.getvalue().splitlines()
    finally:
        access.removeHandler(handler)
        access.setLevel(previous)


class TestUvicornsOwnAccessLine:
    """The record uvicorn itself makes, at the pinned 0.47.0: a request driven
    through its h11 protocol (the only one installed) and formatted by its
    `AccessFormatter`. No socket is bound. A uvicorn upgrade that changes the
    record or this protocol's API fails here, in that upgrade's pull request."""

    @pytest.mark.parametrize(
        "root_path, method, target, printed",
        [
            (
                "",
                "POST",
                f"/api/v1/invitations/{FAKE_TOKEN}/accept",
                "/api/v1/invitations/<redacted>",
            ),
            # A token segment that is empty once its parameters are set aside:
            # Starlette routes it to the accept handler, and uvicorn prints the
            # `;` as `%3B`.
            (
                "",
                "POST",
                f"/api/v1/invitations/;{FAKE_TOKEN}/accept",
                "/api/v1/invitations/<redacted>",
            ),
            (
                "",
                "GET",
                f"/auth/instagram-login/callback?code={FAKE_CODE}&state={FAKE_STATE}",
                "/auth/instagram-login/callback?<redacted>",
            ),
            (
                "",
                "GET",
                f"/webhooks/meta/data-deletion/status?code={FAKE_CAPABILITY}",
                "/webhooks/meta/data-deletion/status?<redacted>",
            ),
            ("", "GET", "/health", "/health"),
            # `--root-path /x` (or UVICORN_ROOT_PATH): uvicorn prints the path
            # with the root path in front of it.
            (
                "/x",
                "POST",
                f"/api/v1/invitations/{FAKE_TOKEN}/accept",
                "/x/api/v1/invitations/<redacted>",
            ),
            # A base URL with a trailing slash (`TARGET_API_URL`) doubles it.
            (
                "",
                "POST",
                f"//api/v1/invitations/{FAKE_TOKEN}/accept",
                "//api/v1/invitations/<redacted>",
            ),
        ],
        ids=[
            "invitation",
            "parameter-token",
            "oauth-callback",
            "deletion-status",
            "plain",
            "root-path",
            "double-slash",
        ],
    )
    async def test_uvicorn_prints_no_query_and_no_token(
        self, access_lines, root_path, method, target, printed
    ):
        from uvicorn.config import Config
        from uvicorn.protocols.http.h11_impl import H11Protocol
        from uvicorn.server import ServerState

        # log_config=None: no dictConfig, so this process's logging is untouched.
        config = Config(
            app=_answer,
            log_config=None,
            proxy_headers=False,
            lifespan="off",
            root_path=root_path,
        )
        state = ServerState()
        protocol = H11Protocol(
            config=config,
            server_state=state,
            app_state={},
            _loop=asyncio.get_running_loop(),
        )
        transport = _FakeTransport()
        protocol.connection_made(transport)
        protocol.data_received(
            f"{method} {target} HTTP/1.1\r\nHost: api.example.test\r\n"
            "Content-Length: 0\r\n\r\n".encode("ascii")
        )
        await asyncio.wait_for(asyncio.gather(*list(state.tasks)), timeout=5)
        protocol.connection_lost(None)  # cancels the keep-alive timer

        assert transport.written.startswith(b"HTTP/1.1 200 OK"), transport.written
        lines = access_lines()
        assert len(lines) == 1, lines
        assert f'10.0.0.7:41000 - "{method} {printed} HTTP/1.1" 200 OK' in lines[0]
        assert [fake for fake in FAKES if fake in lines[0]] == []


class TestUvicornsTraceLine:
    """At trace level (`--log-level trace`, or `UVICORN_LOG_LEVEL=trace`, which
    uvicorn's CLI reads: `uvicorn/main.py:62`), uvicorn wraps the app in
    `MessageLoggerMiddleware` (`uvicorn/config.py:511-512`). It logs each
    request's scope on `uvicorn.asgi` (`uvicorn/middleware/message_logger.py:76-78`).
    This drives that middleware itself."""

    @pytest.mark.parametrize(
        "path, raw_path",
        [
            (
                f"/api/v1/invitations/{FAKE_TOKEN}/accept",
                f"/api/v1/invitations/{FAKE_TOKEN}/accept",
            ),
            # uvicorn decodes `path` and keeps `raw_path` as sent: both of these
            # route to the accept handler, and only `path` reads as its prefix.
            (
                f"/api/v1/invitations/{FAKE_TOKEN}/accept",
                f"/api/v1/invitation%73/{FAKE_TOKEN}/accept",
            ),
            (
                f"/api/v1/invitations/{FAKE_TOKEN}/accept",
                f"/api/v1/invitations%2F{FAKE_TOKEN}/accept",
            ),
            (
                f"/api/v1/invitations/;{FAKE_TOKEN}/accept",
                f"/api/v1/invitations/;{FAKE_TOKEN}/accept",
            ),
        ],
        ids=["plain", "encoded-letter", "encoded-slash", "parameter-token"],
    )
    async def test_the_scope_line_prints_no_query_and_no_token(self, path, raw_path):
        import src.utils.logger  # noqa: F401 — its import installs the redactor
        from uvicorn.logging import TRACE_LOG_LEVEL
        from uvicorn.middleware.message_logger import MessageLoggerMiddleware

        lines: list[str] = []

        class _Grab(logging.Handler):
            def emit(self, record):
                lines.append(record.getMessage())

        grab = _Grab(level=TRACE_LOG_LEVEL)
        asgi = logging.getLogger("uvicorn.asgi")
        previous = asgi.level
        asgi.setLevel(TRACE_LOG_LEVEL)
        asgi.addHandler(grab)
        try:
            scope = {
                "type": "http",
                "method": "POST",
                "client": ("10.0.0.7", 41000),
                "path": path,
                "raw_path": raw_path.encode("ascii"),
                "query_string": f"code={FAKE_CODE}".encode("ascii"),
                "headers": [],
            }

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message):
                pass

            await MessageLoggerMiddleware(_answer)(scope, receive, send)
        finally:
            asgi.removeHandler(grab)
            asgi.setLevel(previous)
        started = [line for line in lines if "Started scope=" in line]
        assert len(started) == 1, lines
        assert "'path': '/api/v1/invitations/<redacted>'" in started[0]
        assert "'raw_path': '<redacted>'" in started[0]
        assert [fake for fake in FAKES if any(fake in line for line in lines)] == []


class TestUvicornsWebSocketLines:
    def test_no_websocket_protocol_can_log_a_handshake(self):
        """uvicorn's WebSocket protocols log each handshake's path and raw query
        string on `uvicorn.error` (`protocols/websockets/websockets_impl.py`,
        `wsproto_impl.py`), which `AccessLogRedactor` does not filter. uvicorn
        loads one only when `websockets` or `wsproto` can be imported, and
        `requirements.txt` pins plain `uvicorn`, so neither is.

        If this fails, a WebSocket library has arrived, perhaps with
        `uvicorn[standard]`: before it ships, put the redaction on those
        records (`REDACTED_LOGGERS`, with a test of their shape) or keep the
        library out."""
        from uvicorn.protocols.websockets.auto import AutoWebSocketsProtocol

        assert AutoWebSocketsProtocol is None


class TestTheHttpLibrariesArePinned:
    def test_httpx_and_httpcore_sit_at_warning_and_do_not_propagate(self):
        from src.utils.logger import LIBRARY_LOG_LEVELS

        assert LIBRARY_LOG_LEVELS == {
            "httpx": logging.WARNING,
            "httpcore": logging.WARNING,
        }
        for name in LIBRARY_LOG_LEVELS:
            library = logging.getLogger(name)
            assert library.level == logging.WARNING, name
            assert library.propagate is False, name

    def test_an_outbound_url_is_not_logged_with_the_root_at_debug(self):
        """The worker's root logger sits at `WORKER_LOG_LEVEL` with a handler
        (`logging.basicConfig`, `src/worker.py`). With the root at DEBUG the same
        way, a request whose URL carries a fake secret leaves no line in any
        handler: the root's, or the library's own."""
        import src.utils.logger  # noqa: F401 — its import pins the libraries

        seen: list[logging.LogRecord] = []

        class _Grab(logging.Handler):
            def emit(self, record):
                seen.append(record)

        grab = _Grab(level=logging.DEBUG)
        root = logging.getLogger()
        listening = [root, logging.getLogger("httpx"), logging.getLogger("httpcore")]
        previous = root.level
        root.setLevel(logging.DEBUG)
        for each in listening:
            each.addHandler(grab)
        try:
            transport = httpx.MockTransport(lambda request: httpx.Response(200))
            with httpx.Client(transport=transport) as client:
                client.get(
                    "https://graph.example.test/refresh_access_token",
                    params={"access_token": "fake-access-token-0000"},
                )
        finally:
            for each in listening:
                each.removeHandler(grab)
            root.setLevel(previous)
        assert [r.name for r in seen if r.name.startswith(("httpx", "httpcore"))] == []
        assert [r for r in seen if "fake-access-token-0000" in r.getMessage()] == []

    def test_httpcore_traces_nothing_with_the_root_at_debug(self):
        """`httpx.MockTransport` never reaches httpcore, so this drives httpcore
        itself: its own pool on its in-memory network backend. httpcore decides
        per call whether to trace by asking its child logger
        `isEnabledFor(logging.DEBUG)` (`httpcore/_trace.py:24`)."""
        import httpcore

        import src.utils.logger  # noqa: F401 — its import pins the libraries

        seen: list[logging.LogRecord] = []

        class _Grab(logging.Handler):
            def emit(self, record):
                seen.append(record)

        grab = _Grab(level=logging.DEBUG)
        root = logging.getLogger()
        previous = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(grab)
        try:
            for name in ("httpcore.connection", "httpcore.http11"):
                enabled = logging.getLogger(name).isEnabledFor(logging.DEBUG)
                assert enabled is False, name
            backend = httpcore.MockBackend(
                [b"HTTP/1.1 200 OK\r\n", b"Content-Length: 0\r\n", b"\r\n"]
            )
            with httpcore.ConnectionPool(network_backend=backend) as pool:
                response = pool.request(
                    "GET",
                    "https://graph.example.test/x?access_token=fake-access-token-0000",
                )
            assert response.status == 200
        finally:
            root.removeHandler(grab)
            root.setLevel(previous)
        assert [r.name for r in seen if r.name.startswith("httpcore")] == []


#: A fresh interpreter prints its answer behind this marker: the app logs to
#: stdout at import, so the answer is the one line that starts with it.
SENTINEL = "LOGGING-AT-IMPORT="

_REPORT = (
    "import json, logging\n"
    "def report():\n"
    "    a = logging.getLogger('uvicorn.access')\n"
    "    s = logging.getLogger('uvicorn.asgi')\n"
    "    pins = {n: [logging.getLogger(n).level, logging.getLogger(n).propagate]"
    " for n in ('httpx', 'httpcore')}\n"
    "    return {'pins': pins, 'handlers': len(a.handlers),"
    " 'filters': [type(f).__name__ for f in a.filters],"
    " 'asgi_filters': [type(f).__name__ for f in s.filters]}\n"
)

#: One access record logged as uvicorn's h11 protocol logs it, with a fake code.
_ACCESS_RECORD = (
    "logging.getLogger('uvicorn.access').info("
    "'%s - \"%s %s HTTP/%s\" %d', '10.0.0.7:41000', 'GET',"
    f" '/auth/google/callback?code={FAKE_CODE}', '1.1', 302)\n"
)


def _fresh(code: str):
    """Run *code* in a fresh interpreter at the repository root. Return the
    JSON it printed behind `SENTINEL`, and its whole stdout. Measured there, as
    `test_the_deployed_entrypoints_pull_no_legacy_module` measures its closure,
    because in this process the answer is whatever an earlier file imported."""
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT
    )
    assert proc.returncode == 0, proc.stderr[-1200:]
    answers = [
        line[len(SENTINEL) :]
        for line in proc.stdout.splitlines()
        if line.startswith(SENTINEL)
    ]
    assert len(answers) == 1, proc.stdout[-1200:]
    return json.loads(answers[0]), proc.stdout


class TestEachProcessIsConfiguredAtImport:
    @pytest.mark.parametrize("root", ["src.api.app", "src.worker"])
    def test_each_deployed_root_pins_the_libraries_and_redacts_the_access_log(
        self, root
    ):
        """The worker reaches `src.utils.logger` only through the services it
        imports (`unit_of_work.py`, `worker_health.py`): this fails if it stops.
        Importing `src.worker` runs nothing (`test_worker_entrypoint.py`)."""
        report, _ = _fresh(
            _REPORT + f"import {root}\nprint({SENTINEL!r} + json.dumps(report()))\n"
        )
        assert report["pins"] == {
            "httpx": [logging.WARNING, False],
            "httpcore": [logging.WARNING, False],
        }
        assert report["filters"].count("AccessLogRedactor") == 1
        assert report["asgi_filters"].count("AccessLogRedactor") == 1

    def test_uvicorns_own_handler_prints_the_redacted_line(self):
        """What `uvicorn src.api.app:app` does before it serves (uvicorn 0.47.0):
        `Config` runs `dictConfig(LOGGING_CONFIG)` (`config.py:284,379`), then
        `config.load_app()` imports the app (`main.py:609`). A record logged then,
        and again after a second `configure_logging()` (a reload child's,
        `_subprocess.py:76`), goes through uvicorn's own stdout `access` handler
        and its `AccessFormatter` (`config.py:78-81,89-93,98`)."""
        code = (
            _REPORT
            + "from uvicorn.config import Config\n"
            + "config = Config('src.api.app:app')\n"
            + "config.load_app()\n"
            + "first = report()\n"
            + _ACCESS_RECORD
            + "config.configure_logging()\n"
            + _ACCESS_RECORD
            + f"print({SENTINEL!r} + json.dumps([first, report()]))\n"
        )
        reports, stdout = _fresh(code)
        for report in reports:
            assert report["filters"].count("AccessLogRedactor") == 1
            assert report["handlers"] == 1, "uvicorn's access handler is configured"
        printed = [
            line for line in stdout.splitlines() if "/auth/google/callback" in line
        ]
        assert len(printed) == 2, stdout[-1200:]
        for line in printed:
            assert '"GET /auth/google/callback?<redacted> HTTP/1.1" 302 Found' in line
        assert FAKE_CODE not in stdout
