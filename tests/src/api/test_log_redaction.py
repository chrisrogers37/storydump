"""What the API's own log lines print of a request's path.

The access log is uvicorn's and is redacted at the logger
(`src/utils/logger.py::AccessLogRedactor`, pinned in
`tests/src/utils/test_logger.py`). These pin the rest: every path parameter is
classified, every credential in a path is masked, every app line that names a
request names its route template (classified values filled in) or its masked
path before routing, and no log line or exception message under `src/` names a
URL or a raw request target.
"""

import ast
import logging
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import TimeoutError as PoolTimeout

from src.api.app import create_app
from src.api.principal import COOKIE
from src.config.settings import settings
from src.exceptions.mix import MixInvalid
from src.exceptions.tenancy import TenantResolutionError, TokenRefused
from tests.src.api.conftest import post_body

SRC = Path(__file__).resolve().parents[3] / "src"

#: An obviously fake invitation token: a line that prints it fails the test.
FAKE_TOKEN = "fake-invite-token-0000"
ACCEPT = f"/api/v1/invitations/{FAKE_TOKEN}/accept"
ACCEPT_TEMPLATE = "/api/v1/invitations/{token}/accept"
FOREIGN_ORIGIN = "https://evil.example.test"
WS = "00000000-0000-0000-0000-000000000001"

#: `{name}` or `{name:convertor}` in a route template.
_PARAM = re.compile(r"\{([^}:]+)(?::[^}]*)?\}")

#: Path parameters whose value is a bearer credential. A route that has one is
#: masked by `redact_path` (`CREDENTIAL_PATH_PREFIXES`, src/utils/logger.py).
#: The printable ones are `LOGGABLE_PATH_PARAMS`, in the source.
CREDENTIAL_PARAMS = frozenset({"token"})


def _templates(app) -> list[str]:
    return [
        route.path
        for route in app.routes
        if isinstance(getattr(route, "path", None), str)
    ]


def _parameters(app) -> set[str]:
    return {name for path in _templates(app) for name in _PARAM.findall(path)}


@pytest.fixture
def app_lines():
    """Every line the app logger emits at INFO or above, as its message. The
    logger does not propagate (`src/utils/logger.py`), so the handler sits on
    it, held at INFO whatever `LOG_LEVEL` this run has (CI sets DEBUG)."""
    from src.utils.logger import logger as app_logger

    lines: list[str] = []

    class _Grab(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    handler = _Grab(level=logging.INFO)
    previous = app_logger.level
    app_logger.setLevel(logging.INFO)
    app_logger.addHandler(handler)
    try:
        yield lines
    finally:
        app_logger.removeHandler(handler)
        app_logger.setLevel(previous)


class TestEveryCredentialInAPathIsMasked:
    """The next route that carries a credential in its path is caught here: its
    parameter is unclassified until someone decides, and a credential parameter
    is unmasked until its prefix joins `CREDENTIAL_PATH_PREFIXES`."""

    def test_every_path_parameter_is_classified(self, app):
        from src.utils.logger import LOGGABLE_PATH_PARAMS

        names = _parameters(app)
        assert names, "no path parameter found: the walk reads nothing"
        assert CREDENTIAL_PARAMS.isdisjoint(LOGGABLE_PATH_PARAMS)
        unclassified = names - CREDENTIAL_PARAMS - LOGGABLE_PATH_PARAMS
        assert unclassified == set(), (
            f"unclassified path parameters {sorted(unclassified)}: a credential "
            "belongs in a header or the body; if it must ride the path, add its "
            "name to CREDENTIAL_PARAMS here and its route's prefix to "
            "CREDENTIAL_PATH_PREFIXES (src/utils/logger.py); otherwise add it to "
            "LOGGABLE_PATH_PARAMS (src/utils/logger.py)"
        )

    def test_no_classification_is_stale(self, app):
        from src.utils.logger import LOGGABLE_PATH_PARAMS

        stale = (CREDENTIAL_PARAMS | LOGGABLE_PATH_PARAMS) - _parameters(app)
        assert stale == set(), f"classified, but no route uses them: {sorted(stale)}"

    def test_every_route_with_a_credential_parameter_is_masked(self, app):
        from src.utils.logger import REDACTED, redact_path, redact_target

        checked = []
        for template in _templates(app):
            names = _PARAM.findall(template)
            secret_names = CREDENTIAL_PARAMS.intersection(names)
            if not secret_names:
                continue
            sample = _PARAM.sub(lambda m: f"sentinel-{m.group(1)}", template)
            for printed in (
                redact_path(sample),
                redact_target(f"{sample}?code=sentinel-query"),
            ):
                assert REDACTED in printed, (template, printed)
                assert "sentinel-query" not in printed, (template, printed)
                for name in secret_names:
                    assert f"sentinel-{name}" not in printed, (template, printed)
            checked.append(template)
        # Closed on purpose: a second credential route is a decision a reviewer sees.
        assert checked == [ACCEPT_TEMPLATE], checked


class TestTheAppsLinesNameTheRoute:
    def test_an_unsigned_accept_is_logged_by_its_template(self, client, app_lines):
        resp = client.post(ACCEPT)
        assert resp.status_code == 401
        assert any(ACCEPT_TEMPLATE in line for line in app_lines), app_lines
        assert [line for line in app_lines if FAKE_TOKEN in line] == []

    def test_a_cross_site_accept_names_the_template_in_its_log_and_its_body(
        self, client, app_lines
    ):
        client.cookies.set(COOKIE, "opaque")
        resp = client.post(ACCEPT, headers={"Origin": FOREIGN_ORIGIN})
        assert resp.status_code == 403 and resp.json()["reason"] == "cross_site"
        assert ACCEPT_TEMPLATE in resp.json()["detail"]
        assert FAKE_TOKEN not in resp.text
        assert any(ACCEPT_TEMPLATE in line for line in app_lines), app_lines
        assert [line for line in app_lines if FAKE_TOKEN in line] == []

    def test_a_refusal_line_keeps_the_workspace_and_the_command(
        self, client, app_lines
    ):
        """`ws` and `command` are `LOGGABLE_PATH_PARAMS`, so the line still says
        which workspace and which command were refused (passes on main too, where
        the raw path prints; it fails if the template ever drops them)."""
        resp = client.post(f"/api/v1/workspaces/{WS}/commands/approve")
        assert resp.status_code == 401
        expected = f"refused POST /api/v1/workspaces/{WS}/commands/approve:"
        assert any(expected in line for line in app_lines), app_lines

    @pytest.mark.parametrize(
        "refusal, status, says",
        [
            (
                lambda: TenantResolutionError("unknown_binding"),
                500,
                "unmapped refusal on GET",
            ),
            (lambda: TokenRefused("session_required", "a probe"), 403, "refused GET"),
            (PoolTimeout, 503, "pool saturated on GET"),
            (lambda: MixInvalid("bad_ratio"), 400, "refused GET"),
        ],
        ids=["unmapped", "mapped", "pool", "mix"],
    )
    def test_every_handler_that_logs_names_the_template(
        self, app, client, app_lines, refusal, status, says
    ):
        async def probe(token: str):
            raise refusal()

        app.add_api_route("/_test/{token}/probe", probe)
        resp = client.get(f"/_test/{FAKE_TOKEN}/probe")
        assert resp.status_code == status, resp.text
        expected = f"{says} /_test/{{token}}/probe"
        assert any(expected in line for line in app_lines), app_lines
        assert [line for line in app_lines if FAKE_TOKEN in line] == []

    def test_an_over_limit_body_is_refused_by_its_masked_path(
        self, monkeypatch, app_lines
    ):
        """The body limit runs before routing, so there is no template yet: the
        raw path goes through `redact_path`."""
        monkeypatch.setattr(settings, "API_REQUEST_BODY_MAX_BYTES", 1024)
        client = TestClient(create_app(env={}))
        resp = post_body(client, ACCEPT, b"x" * 1025)
        assert resp.status_code == 413
        expected = "refused POST /api/v1/invitations/<redacted>/accept from"
        assert any(expected in line for line in app_lines), app_lines
        assert [line for line in app_lines if FAKE_TOKEN in line] == []


# --- the ratchet: no log line or exception message names a request's target ---

#: The logging methods a module logger is called with.
_LOG_METHODS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
)
#: Attributes that hold a URL or a request's parsed target, whatever owns them:
#: `request.url` and everything under it, an httpx response's or error's `.url`
#: and `.request.url`, Starlette's query and path parameters.
_URL_ATTRIBUTES = frozenset({"url", "query_params", "path_params"})
#: The ASGI scope keys that hold the raw request target or its parameters.
_SCOPE_KEYS = frozenset({"path", "raw_path", "query_string", "path_params"})

#: The functions that may read a raw request target, each with its reason: the
#: masks `src/utils/logger.py` defines. `request.base_url` (principal.py) and
#: `request.url_for` (meta.py) need no entry: the checks match the attribute
#: names exactly.
RAW_TARGET_READERS = {
    ("src/utils/logger.py", "loggable_path"): "prints the template, classified "
    "values and a masked path only",
    ("src/utils/logger.py", "_redact_scope"): "masks the scope uvicorn's trace "
    "line prints",
}


def _is_scope(node: ast.AST) -> bool:
    return (isinstance(node, ast.Name) and node.id == "scope") or (
        isinstance(node, ast.Attribute) and node.attr == "scope"
    )


def _is_scope_key(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value in _SCOPE_KEYS


def _reads_raw_target(node: ast.AST) -> bool:
    """`scope["path"]`, `scope.get("query_string")`, `request.scope["raw_path"]`,
    and `request.url`, `request.query_params` or `request.path_params`."""
    if isinstance(node, ast.Subscript) and _is_scope(node.value):
        return _is_scope_key(node.slice)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and _is_scope(node.func.value)
        and node.args
    ):
        return _is_scope_key(node.args[0])
    return (
        isinstance(node, ast.Attribute)
        and node.attr in _URL_ATTRIBUTES
        and isinstance(node.value, ast.Name)
        and node.value.id == "request"
    )


def _names_a_url(node: ast.AST) -> bool:
    url = isinstance(node, ast.Attribute) and node.attr in _URL_ATTRIBUTES
    return url or _reads_raw_target(node)


def _is_message(node: ast.AST, raised: set[int]) -> bool:
    """A logging call, what a `raise` raises, or an exception built by name."""
    if not isinstance(node, ast.Call):
        return False
    if id(node) in raised:
        return True
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in _LOG_METHODS:
        return True
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    return name.endswith(("Error", "Exception", "Refused"))


def _walk(tree: ast.AST):
    """Every node, with the name of the innermost function around it ('' at
    module level)."""
    stack = [(tree, "")]
    while stack:
        node, where = stack.pop()
        yield node, where
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            where = node.name
        stack.extend((child, where) for child in ast.iter_child_nodes(node))


def _message_hits(tree: ast.AST) -> list[tuple[int, str]]:
    raised = {
        id(node.exc)
        for node in ast.walk(tree)
        if isinstance(node, ast.Raise) and node.exc is not None
    }
    hits = []
    for node, where in _walk(tree):
        if not _is_message(node, raised):
            continue
        for part in [*node.args, *(keyword.value for keyword in node.keywords)]:
            hits += [(n.lineno, where) for n in ast.walk(part) if _names_a_url(n)]
    return hits


def _raw_reads(tree: ast.AST) -> list[tuple[int, str]]:
    return [
        (node.lineno, where) for node, where in _walk(tree) if _reads_raw_target(node)
    ]


def _sources():
    for path in sorted(SRC.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        yield path.relative_to(SRC.parent).as_posix(), ast.parse(source)


class TestNoLogLineCarriesARequestTarget:
    """A ratchet over `src/`: a new log line or exception message that names a
    URL or a raw request target fails here, not in a production log."""

    def test_no_log_line_or_exception_message_names_a_url_or_a_raw_target(self):
        found = [
            f"{rel}:{line} in {where or '<module>'}"
            for rel, tree in _sources()
            for line, where in _message_hits(tree)
        ]
        assert found == [], (
            "a log line or an exception message names a URL or a request's raw "
            "target: name the request by `loggable_path(request.scope)` "
            f"(src/utils/logger.py), and redact a provider URL first: {found}"
        )

    def test_only_the_masks_read_a_raw_target(self):
        found = sorted(
            f"{rel}:{line} in {where or '<module>'}"
            for rel, tree in _sources()
            for line, where in _raw_reads(tree)
            if (rel, where) not in RAW_TARGET_READERS
        )
        assert found == [], (
            "code outside the masks reads a request's raw target, and a log line "
            f"built from it would print a credential: {found}"
        )

    def test_every_allowed_reader_still_reads(self):
        readers = {
            (rel, where) for rel, tree in _sources() for _, where in _raw_reads(tree)
        }
        stale = set(RAW_TARGET_READERS) - readers
        assert stale == set(), f"allowed, but no longer reading: {sorted(stale)}"

    def test_the_ratchet_sees_a_planted_leak(self):
        planted = ast.parse(
            "def leak(request, scope, response):\n"
            "    logger.info('callback %s', request.url)\n"
            "    logger.warning(f'refused {scope[\"path\"]}')\n"
            "    raise ValueError(str(response.request.url))\n"
            "    where = scope.get('query_string')\n"
        )
        assert sorted(_message_hits(planted)) == [(2, "leak"), (3, "leak"), (4, "leak")]
        assert sorted(_raw_reads(planted)) == [(2, "leak"), (3, "leak"), (5, "leak")]
