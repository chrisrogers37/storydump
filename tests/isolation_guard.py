"""The isolated provider gate's pre-connection guard (#1644).

The Instagram source gate composes the real services against a throwaway
PostgreSQL with fake providers. This plugin makes "fake providers only" a
property of the run instead of a promise. It is loaded explicitly, and only a
command-line flag arms it::

    PYTHONPATH="$PWD" python -m pytest -p tests.isolation_guard \\
        --isolated-provider-gate <the gate's test file>

**Why a flag.** Anything else that could mark the mode is what the guard has
to distrust: ``tests/conftest.py`` runs ``load_dotenv(".env.test",
override=True)``, so a marker kept in the environment can be unset by the very
file under suspicion. ``sys.argv`` cannot be. Without the flag, importing this
module changes nothing, so every normal run is unaffected.

**Three layers, each in place before the next could matter.**

1. ``pytest_load_initial_conftests`` runs before any conftest is imported. It
   checks that this file came from the checkout under test (the shared
   virtualenv's editable install can resolve ``tests`` to another checkout),
   refuses a ``.env`` (pydantic settings load ``./.env`` implicitly, so the
   environment alone cannot say what configuration a run would use), and
   installs the deny guard below.
2. ``validate_effective_configuration`` is called from ``tests/conftest.py``
   right after the ``.env.test`` override and before settings are imported, so
   it judges the *effective* environment, and it precedes ``server_answered``,
   ``setup_test_database`` and every other path that opens a connection. It
   names variables and never prints a value.
3. The deny guard refuses what escapes the first two: a socket that is not the
   disposable PostgreSQL (loopback, port 65433) or a registered loopback
   fixture, name resolution for anything but localhost, a ``psycopg2``
   connection (libpq does not use Python sockets) whose parameters are not the
   disposable cluster's, a real ``httpx`` transport, the Cloudinary SDK's
   network modules, and the worker's entrypoints.

``IsolationViolation`` is a ``BaseException``, as pytest's own ``Failed`` is:
a provider adapter or retry loop that answers ``except Exception`` must not be
able to turn a refused live call into a quiet fallback. Every refusal is also
recorded, and the session fails if any was recorded, so a handler that
swallows even this cannot produce a green run.

Not covered: a ``psql`` subprocess is its own libpq client. The fixtures hand
it the DSN the settings build, which layer 2 has pinned to the disposable
cluster, plus ``PGHOSTADDR``/``PGSERVICE``, which layer 2 refuses.
"""

from __future__ import annotations

import functools
import importlib.abc
import importlib.machinery
import os
import re
import socket
import sys
from pathlib import Path
from typing import Callable, Iterable, NoReturn, Optional
from urllib.parse import parse_qs, urlsplit

import pytest

#: The command-line marker. Registered here, so passing it without
#: ``-p tests.isolation_guard`` is pytest's own "unrecognized arguments".
OPTION = "--isolated-provider-gate"

#: Set by ``pytest_load_initial_conftests`` once OPTION is seen; until then
#: ``validate_effective_configuration`` is a no-op.
ACTIVE = False

#: The one TCP port the isolated gate may reach: the disposable PostgreSQL.
DISPOSABLE_PORT = 65433

#: Hosts that mean "this machine". An exact set, not ``127.0.0.0/8``: one
#: spelling is easy to reason about and to refuse everything else against.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

#: Every refusal, in order. ``pytest_sessionfinish`` fails the run if any
#: exists, however the code under test handled the exception.
VIOLATIONS: list[str] = []

#: ``.env.example`` names every variable something in the tree reads; the
#: effective-configuration rule is derived from it so a new variable is
#: covered the day it is documented.
ENV_EXAMPLE = Path(__file__).resolve().parent.parent / ".env.example"

#: Database and logging settings the isolated gate sets or tolerates. The
#: database ones are checked by their own rules below.
_KEPT_EXACT = frozenset(
    {
        "TEST_DB_NAME",
        "ENCRYPTION_KEY",
        "REQUIRE_TEST_DATABASE",
        "LOG_LEVEL",
        "WORKER_LOG_LEVEL",
    }
)
_KEPT_PREFIX = "DB_"

#: Connection-string variables: absent, or a loopback URL on the disposable
#: port. Checked as URLs, so they are exempt from the plain "unset" rule.
DSN_VARIABLES = (
    "TARGET_DATABASE_URL",
    "DATABASE_URL",
    "TEST_DATABASE_URL",
    "SCHEMA_DRIFT_DSN",
)

#: libpq reads these on top of the parameters a caller passes: ``hostaddr``
#: wins over ``host`` for the actual connection, and a service entry can
#: supply one. A shell that carries either can redirect a connection whose
#: DSN looks loopback.
LIBPQ_REDIRECTS = ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE")

#: Query parameters that replace the part of a URL the host and port checks
#: read.
_DSN_OVERRIDES = frozenset({"host", "hostaddr", "port", "service", "servicefile"})

#: The databases and roles of the disposable cluster, by the conventions the
#: suite's own fixtures use (``tests/conftest.py``, ``tests/scripts/conftest.py``).
#: A name outside them is refused loudly, so a new fixture family is a visible
#: edit here, not a silent widening.
_DATABASE_NAME = re.compile(
    r"postgres|storyline_ai|storyline_test[a-z0-9_]*|runner_(?:test|tpl)_[a-z0-9_]+"
)
_ROLE_NAME = re.compile(r"test_user|svc_[a-z_]+|lineage_owner_[a-z0-9_]+")

#: Modules whose import is refused outright.
DENIED_IMPORTS = {
    "src.main": "it is the posting worker's deployed entrypoint",
    "cloudinary.uploader": "the Cloudinary SDK's upload module is a live provider client",
    "cloudinary.api": "the Cloudinary SDK's admin module is a live provider client",
}

#: Ports registered by ``allow_loopback``, beside the database's.
_fixture_ports: set[int] = set()

#: Undo actions for ``install``, in install order.
_undo: list[Callable[[], None]] = []


class IsolationViolation(BaseException):
    """A database or provider effect the isolated gate refused.

    A ``BaseException`` on purpose; see the module docstring.
    """


def _refuse(message: str) -> NoReturn:
    full = f"isolated provider gate: {message}"
    VIOLATIONS.append(full)
    raise IsolationViolation(full)


def _require(condition: bool, message: str) -> None:
    if not condition:
        _refuse(message)


def _loopback(host: object) -> bool:
    if isinstance(host, bytes):
        host = host.decode("latin-1")
    return isinstance(host, str) and host.lower() in LOOPBACK_HOSTS


def allow_loopback(port: int) -> None:
    """Register an in-process fixture server's loopback port.

    Sockets and ``httpx`` may then reach that port on a loopback host. The
    database's own port needs no registration.
    """
    _fixture_ports.add(int(port))


def _socket_permitted(address: object) -> bool:
    # AF_INET is (host, port); AF_INET6 adds flowinfo and scope. Anything else,
    # an AF_UNIX path included, is not the disposable PostgreSQL.
    if not isinstance(address, tuple) or len(address) < 2:
        return False
    try:
        port = int(address[1])
    except (TypeError, ValueError):
        return False
    return _loopback(address[0]) and (port == DISPOSABLE_PORT or port in _fixture_ports)


# --- layer 2: the effective configuration ----------------------------------------------


def example_names() -> set[str]:
    """Every ``NAME=`` in ``.env.example``, commented out or not: a variable
    the file tells a reader to set."""
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    return set(re.findall(r"^\s*#?\s*([A-Z][A-Z0-9_]*)=", text, re.M))


def _dsn_problem(value: str) -> Optional[str]:
    """Why this is not a loopback URL on the disposable port, or None. Names a
    part of the URL, never what the part holds."""
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return "is not a parseable URL"
    if not parts.scheme.startswith("postgres") or not parts.netloc:
        return "is not a postgresql:// URL, so its host cannot be checked"
    if parts.netloc.count("@") > 1:
        return "has an unencoded @ in its credentials, which parsers read differently"
    if {k.lower() for k in parse_qs(parts.query)} & _DSN_OVERRIDES:
        return "replaces its host, port or service in the query string"
    if not _loopback(parts.hostname):
        return "names a host other than localhost"
    if port != DISPOSABLE_PORT:
        return f"does not use port {DISPOSABLE_PORT}"
    return None


def _is_fernet_key(raw: str) -> bool:
    from cryptography.fernet import Fernet

    try:
        Fernet(raw)
    except (TypeError, ValueError):
        return False
    return True


def _configuration_problems(env) -> list[tuple[str, str]]:
    problems: list[tuple[str, str]] = []

    def value(name: str) -> Optional[str]:
        raw = env.get(name)
        return raw if raw is not None and raw.strip() else None

    def need(name: str, ok: bool, why: str) -> None:
        if not ok:
            problems.append((name, why))

    host = value("DB_HOST")
    need("DB_HOST", _loopback(host), "must be localhost, 127.0.0.1 or ::1")
    port = value("DB_PORT")
    need(
        "DB_PORT",
        port is not None
        and re.fullmatch(r"[0-9]+", port) is not None
        and int(port) == DISPOSABLE_PORT,
        f"must be the disposable test cluster's port, {DISPOSABLE_PORT}",
    )
    need("DB_USER", value("DB_USER") == "test_user", "must be test_user")
    name = value("TEST_DB_NAME")
    need(
        "TEST_DB_NAME",
        name is not None and name.startswith("storyline_test"),
        "must start with storyline_test",
    )
    name = value("DB_NAME")
    need(
        "DB_NAME",
        name is not None
        and (name == "storyline_ai" or name.startswith("storyline_test")),
        "must be storyline_ai or start with storyline_test",
    )
    key = value("ENCRYPTION_KEY")
    need(
        "ENCRYPTION_KEY",
        key is not None and _is_fernet_key(key),
        "must be a valid Fernet key (generate a fresh, test-only one)",
    )

    for dsn in DSN_VARIABLES:
        raw = value(dsn)
        if raw is not None:
            why = _dsn_problem(raw)
            need(dsn, why is None, why or "")

    try:
        listed = example_names()
    except OSError:
        listed = set()
        problems.append(
            (
                ".env.example",
                "could not be read, so the variables to refuse are unknown",
            )
        )
    kept = _KEPT_EXACT | set(DSN_VARIABLES)
    for var in sorted(listed | set(LIBPQ_REDIRECTS)):
        if var in kept or var.startswith(_KEPT_PREFIX):
            continue
        need(var, value(var) is None, "must be unset for the isolated gate")
    return sorted(problems)


def _refusal(headline: str, problems: Iterable[tuple[str, str]]) -> str:
    lines = [
        f"isolated provider gate refused to start ({headline}); nothing was connected:"
    ]
    lines += [f"  {name}: {why}" for name, why in problems]
    lines.append(f"Fix these, or run without {OPTION}.")
    return "\n".join(lines)


def validate_effective_configuration(env=os.environ) -> None:
    """Refuse, by variable name and never by value, an environment the isolated
    gate must not run in. A no-op unless the gate is armed.

    Called by ``tests/conftest.py`` between its ``.env.test`` override and its
    settings import, so ``env`` is the configuration the run would really use.
    """
    if not ACTIVE:
        return
    problems = _configuration_problems(env)
    if problems:
        raise pytest.UsageError(_refusal("effective configuration", problems))


# --- layer 1: the plugin ---------------------------------------------------------------


def pytest_addoption(parser) -> None:
    parser.addoption(
        OPTION,
        action="store_true",
        default=False,
        dest="isolated_provider_gate",
        help="Refuse any database or provider other than the disposable "
        "PostgreSQL on loopback; needs -p tests.isolation_guard.",
    )


def _within(path: Path, root: Path) -> bool:
    # Both spellings of each side: a symlinked rootdir and a logical $PWD must
    # not read as another checkout, and another checkout must never read as this.
    for candidate in {Path(os.path.abspath(path)), path.resolve()}:
        for base in {Path(os.path.abspath(root)), root.resolve()}:
            if candidate == base or base in candidate.parents:
                return True
    return False


def _checkout_problems(rootpath: Path, cwd: Path) -> list[tuple[str, str]]:
    problems: list[tuple[str, str]] = []
    if not _within(Path(__file__), rootpath):
        problems.append(
            (
                "tests.isolation_guard",
                "was imported from a different checkout than the rootdir under test;"
                ' run PYTHONPATH="$PWD" python -m pytest from the checkout',
            )
        )
    places: dict[str, str] = {}
    for label, directory in (("the working directory", cwd), ("the rootdir", rootpath)):
        places.setdefault(os.path.realpath(directory), label)
    for directory, label in places.items():
        if (Path(directory) / ".env").is_file():
            problems.append(
                (
                    ".env",
                    f"exists in {label}; pydantic settings load it on import, so the"
                    " effective configuration cannot be known",
                )
            )
    return problems


@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(early_config) -> None:
    global ACTIVE
    if not getattr(early_config.known_args_namespace, "isolated_provider_gate", False):
        return
    ACTIVE = True
    problems = _checkout_problems(Path(early_config.rootpath), Path.cwd())
    if problems:
        raise pytest.UsageError(_refusal("checkout", problems))
    install()


def pytest_sessionfinish(session, exitstatus) -> None:
    if not (ACTIVE and VIOLATIONS):
        return
    print(f"\n{len(VIOLATIONS)} effect(s) refused by the isolated provider gate:")
    for message in dict.fromkeys(VIOLATIONS):
        print(f"  {message}")
    session.exitstatus = pytest.ExitCode.TESTS_FAILED


# --- layer 3: the deny guard -----------------------------------------------------------


def _swap(owner: object, name: str, make: Callable[[Callable], Callable]) -> None:
    """Replace ``owner.name`` with ``make(original)`` and remember the undo."""
    own = name in vars(owner)
    original = getattr(owner, name)
    setattr(owner, name, make(original))

    def undo() -> None:
        if own:
            setattr(owner, name, original)
        else:
            delattr(owner, name)

    _undo.append(undo)


def _guard_sockets() -> None:
    def connecting(original):
        @functools.wraps(original)
        def guarded(self, address):
            _require(
                _socket_permitted(address),
                f"a socket connect other than loopback port {DISPOSABLE_PORT}"
                " or a registered loopback fixture was refused",
            )
            return original(self, address)

        return guarded

    def creating(original):
        @functools.wraps(original)
        def guarded(address, *args, **kwargs):
            _require(
                _socket_permitted(address),
                f"a socket connect other than loopback port {DISPOSABLE_PORT}"
                " or a registered loopback fixture was refused",
            )
            return original(address, *args, **kwargs)

        return guarded

    def resolving(original):
        @functools.wraps(original)
        def guarded(host, *args, **kwargs):
            _require(
                host is None or host == "" or _loopback(host),
                "name resolution for a host other than localhost was refused",
            )
            return original(host, *args, **kwargs)

        return guarded

    _swap(socket.socket, "connect", connecting)
    _swap(socket.socket, "connect_ex", connecting)
    _swap(socket, "create_connection", creating)
    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex"):
        _swap(socket, name, resolving)


def _libpq_problem(params: dict) -> Optional[str]:
    """The first libpq parameter that is not the disposable cluster's, or None."""

    def listed(name: str) -> list[str]:
        return [part.strip() for part in params.get(name, "").split(",")]

    if params.get("service") or params.get("servicefile"):
        return "service"
    hosts = listed("host")
    if not params.get("host") or not all(_loopback(h) for h in hosts):
        return "host"
    if "hostaddr" in params and not all(_loopback(h) for h in listed("hostaddr")):
        return "hostaddr"
    ports = listed("port")
    if not params.get("port") or any(p != str(DISPOSABLE_PORT) for p in ports):
        return "port"
    if not _DATABASE_NAME.fullmatch(params.get("dbname", "")):
        return "dbname"
    if not _ROLE_NAME.fullmatch(params.get("user", "")):
        return "user"
    return None


def _guard_psycopg2() -> None:
    try:
        import psycopg2
        from psycopg2.extensions import make_dsn, parse_dsn
    except ImportError:
        return

    def connecting(original):
        @functools.wraps(original)
        def guarded(dsn=None, connection_factory=None, cursor_factory=None, **kwargs):
            asynchronous = {
                k: kwargs.pop(k) for k in ("async", "async_") if k in kwargs
            }
            merged = parse_dsn(make_dsn(dsn, **kwargs))
            wrong = _libpq_problem(merged)
            _require(
                wrong is None,
                f"psycopg2.connect was refused: its {wrong} is not the disposable"
                f" test cluster's (loopback, port {DISPOSABLE_PORT})",
            )
            return original(
                dsn,
                connection_factory,
                cursor_factory,
                **kwargs,
                **asynchronous,
            )

        return guarded

    _swap(psycopg2, "connect", connecting)


def _guard_httpx() -> None:
    try:
        import httpx
    except ImportError:
        return

    def permitted(url: "httpx.URL") -> bool:
        port = url.port or {"http": 80, "https": 443}.get(url.scheme)
        return _loopback(url.host) and port in _fixture_ports

    refusal = (
        "a request through a real httpx transport was refused (in-process transports"
        " and registered loopback fixtures only)"
    )

    def sync(original):
        @functools.wraps(original)
        def guarded(self, request):
            _require(permitted(request.url), refusal)
            return original(self, request)

        return guarded

    def asynchronous(original):
        @functools.wraps(original)
        async def guarded(self, request):
            _require(permitted(request.url), refusal)
            return await original(self, request)

        return guarded

    _swap(httpx.HTTPTransport, "handle_request", sync)
    _swap(httpx.AsyncHTTPTransport, "handle_async_request", asynchronous)


def _refusing_entrypoint(name: str) -> Callable:
    def refuse(*args, **kwargs):
        _refuse(f"src.worker.{name} was refused: it starts the posting worker")

    refuse.__name__ = name
    return refuse


def _forbid_worker_entrypoints(module) -> None:
    for name in ("main", "run"):
        setattr(module, name, _refusing_entrypoint(name))


#: Modules patched right after their import.
PATCHED_AFTER_IMPORT: dict[str, Callable] = {"src.worker": _forbid_worker_entrypoints}


class _DenyLoader(importlib.abc.Loader):
    def __init__(self, name: str, reason: str) -> None:
        self._name, self._reason = name, reason

    def create_module(self, spec):
        return None

    def exec_module(self, module) -> None:
        _refuse(f"importing {self._name} was refused: {self._reason}")


class _PatchingLoader:
    """The real loader, then ``patch``. Anything else a loader is asked
    (``get_source`` for a traceback, say) goes to the real one."""

    def __init__(self, loader, patch: Callable) -> None:
        self._loader, self._patch = loader, patch

    def create_module(self, spec):
        create = getattr(self._loader, "create_module", None)
        return create(spec) if create else None

    def exec_module(self, module) -> None:
        self._loader.exec_module(module)
        self._patch(module)

    def __getattr__(self, name: str):
        return getattr(self._loader, name)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in DENIED_IMPORTS:
            loader = _DenyLoader(fullname, DENIED_IMPORTS[fullname])
            return importlib.machinery.ModuleSpec(fullname, loader)
        patch = PATCHED_AFTER_IMPORT.get(fullname)
        if patch is None:
            return None
        for finder in sys.meta_path:
            find = getattr(finder, "find_spec", None)
            if finder is self or find is None:
                continue
            spec = find(fullname, path, target)
            if spec is not None and spec.loader is not None:
                spec.loader = _PatchingLoader(spec.loader, patch)
                return spec
        return None


def _guard_imports() -> None:
    finder = _Finder()
    sys.meta_path.insert(0, finder)
    _undo.append(lambda: sys.meta_path.remove(finder))


def install() -> None:
    """Install the deny guard. Idempotent. The plugin calls it before any
    conftest, hence before ``src`` is imported, which the import hooks need."""
    if _undo:
        return
    _guard_sockets()
    _guard_psycopg2()
    _guard_httpx()
    _guard_imports()


def uninstall() -> None:
    """Restore everything ``install`` replaced. For this module's own tests; a
    gate run never lifts the guard."""
    while _undo:
        _undo.pop()()
    _fixture_ports.clear()
