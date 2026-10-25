"""Logging configuration, and what a log line may print about a request.

`setup_logger` builds the shared `logger`. Importing this module also does two
things every process that logs through it needs before it serves a request or
calls a provider:

- it pins the HTTP libraries' loggers at WARNING (`LIBRARY_LOG_LEVELS`): httpx
  prints every outbound URL at INFO, and a provider URL carries a secret;
- it puts `AccessLogRedactor` on uvicorn's access logger and on its trace-level
  ASGI logger (`REDACTED_LOGGERS`), so neither prints a query string, nor
  anything after a `CREDENTIAL_PATH_PREFIXES` prefix in a path.

Both composition roots import it: the API through `src/api/app.py`, the worker
through the services it imports (`src/services/target/unit_of_work.py`).
`tests/src/utils/test_logger.py` holds each root to both, in a fresh
interpreter.
"""

import logging
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import quote

from src.config.settings import settings


def setup_logger(
    name: str = "storydump", level: int = None, log_file: Optional[str] = None
) -> logging.Logger:
    """
    Setup and configure a logger.

    Args:
        name: Logger name
        level: Logging level (defaults to settings.LOG_LEVEL)
        log_file: Path to log file (defaults to logs/app.log)

    Returns:
        Configured logger instance
    """
    # Create logs directory if it doesn't exist
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    # Get or create logger
    logger_instance = logging.getLogger(name)

    # Set level
    if level is None:
        level = getattr(logging, settings.LOG_LEVEL)
    logger_instance.setLevel(level)

    # Avoid adding duplicate handlers
    if logger_instance.handlers:
        return logger_instance

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_format = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console_handler.setFormatter(console_format)

    # File handler
    if log_file is None:
        log_file = str(log_dir / "app.log")

    file_path = Path(log_file)
    file_path.parent.mkdir(parents=True, exist_ok=True)

    file_handler = logging.FileHandler(file_path)
    file_handler.setLevel(logging.INFO)
    file_format = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(file_format)

    # Add handlers
    logger_instance.addHandler(console_handler)
    logger_instance.addHandler(file_handler)

    # Prevent propagation to root logger
    logger_instance.propagate = False

    return logger_instance


# Default logger instance
logger = setup_logger()

#: The HTTP libraries' loggers, and the level each is pinned at. httpx logs
#: every request at INFO with its whole URL ("HTTP Request: GET <url> ..."),
#: and a provider URL carries a secret, such as the bot token in a Telegram path
#: (`src/channels/telegram_transport.py`), a client secret or an access token
#: in an OAuth or Graph query (`ig_login_oauth.py`, `credential_lifecycle.py`).
#: httpcore logs at DEBUG. The worker's root logger sits at `WORKER_LOG_LEVEL`
#: (`logging.basicConfig` in `src/worker.py`), so an unpinned library would
#: print through it.
LIBRARY_LOG_LEVELS: dict[str, int] = {
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
}

# Route the pinned libraries' logs through our logger's handlers so they appear
# in Railway logs (otherwise they go to the root logger / stderr). No Telegram
# SDK is installed, so httpx and httpcore are the libraries routed (#1216).
for _lib_logger_name, _lib_level in LIBRARY_LOG_LEVELS.items():
    _lib_logger = logging.getLogger(_lib_logger_name)
    _lib_logger.setLevel(_lib_level)
    for _handler in logger.handlers:
        _lib_logger.addHandler(_handler)
    _lib_logger.propagate = False

#: What a log line prints in place of a query string or a credential.
REDACTED = "<redacted>"

#: The paths a credential follows. Wherever one of them appears in a path a log
#: line prints, `redact_path` prints everything after it as one `REDACTED`. A
#: route that has to carry a credential in its path is added here, and
#: `tests/src/api/test_log_redaction.py` fails until it is; a new credential
#: belongs in a header or the body instead.
CREDENTIAL_PATH_PREFIXES: tuple[str, ...] = (
    # POST /api/v1/invitations/{token}/accept: the invitation token, a 7-day
    # bearer credential for joining a workspace (`src/api/routes/v1.py`).
    "/api/v1/invitations/",
    # The site's invitation link, /join/{token} (`landing/src/app/join/`). The
    # API serves no such route, so a request for one is a misdirected link.
    "/join/",
)

#: Each of `CREDENTIAL_PATH_PREFIXES` as the segment names `redact_path` matches.
_CREDENTIAL_SEGMENTS: tuple[tuple[str, ...], ...] = tuple(
    tuple(prefix.strip("/").split("/")) for prefix in CREDENTIAL_PATH_PREFIXES
)

#: Segments that name nothing (`//`, `/./`): a prefix is matched across them.
_EMPTY_SEGMENTS = frozenset({"", "."})

#: Path parameters whose value an application log line may print. Each names a
#: row, a key or a command, which the caller reaches only through its own
#: membership or token. Every other parameter prints as its `{name}`, so a new
#: one, a credential above all, stays out until it is classified here;
#: `tests/src/api/test_log_redaction.py` fails until every route's parameters
#: are classified.
LOGGABLE_PATH_PARAMS = frozenset(
    {
        "account_id",
        "binding_id",
        "command",
        "intent_id",
        "invitation_id",
        "key",
        "media_id",
        "result_id",
        "source",
        "source_id",
        "token_id",
        "ws",
    }
)

#: A route template's `{name}` or `{name:convertor}`.
_PATH_PARAM = re.compile(r"\{([^}:]+)(?::[^}]*)?\}")

#: The uvicorn loggers whose records carry a request's target: the access
#: line, and the ASGI trace that `--log-level trace` (or
#: `UVICORN_LOG_LEVEL=trace`) switches on.
REDACTED_LOGGERS: tuple[str, ...] = ("uvicorn.access", "uvicorn.asgi")


def _name_length(segment: str) -> int:
    """How much of *segment* is its name: up to its first parameter (``;p=1``,
    which uvicorn's access line prints as ``%3Bp%3D1``), or all of it."""
    marks = (segment.find(mark) for mark in (";", "%3B", "%3b"))
    return min((at for at in marks if at >= 0), default=len(segment))


def _segment_name(segment: str) -> str:
    """*segment* as a prefix is compared with it: its name, lowercased."""
    return segment[: _name_length(segment)].lower()


def _prefix_end(names: list, named: list, segments: tuple) -> Optional[int]:
    """Where *segments* first ends in a path split into *names*, matched over
    the *named* positions only (so across empty and ``.`` segments), or None."""
    size = len(segments)
    for start in range(len(named) - size + 1):
        if all(names[named[start + k]] == segments[k] for k in range(size)):
            return named[start + size - 1]
    return None


def redact_path(path: str) -> str:
    """*path* with everything after its first credential prefix as one
    `REDACTED`.

    ``/api/v1/invitations/<token>/accept`` becomes
    ``/api/v1/invitations/<redacted>``, and ``/join/<token>/start`` becomes
    ``/join/<redacted>``: the token, whatever its segment looks like, and
    every segment after it. The prefixes of `CREDENTIAL_PATH_PREFIXES` are
    matched as whole segments anywhere in the path, in any letter case,
    whatever parameters a segment carries (``;p=1``) and across empty and
    ``.`` segments, so a root path in front (``/x/api/v1/...``), a doubled
    slash (``//api/v1/...``) and an absolute-form target
    (``http%3A//host/api/v1/...``) are masked too. A path that ends at its
    prefix (``/api/v1/invitations/``) has nothing after it to mask; a path that
    carries no prefix (``/api/v1/me/invitations/<id>/resend``) is unchanged;
    and redacting twice changes nothing.
    """
    lowered = path.lower()
    if not any(segments[-1] in lowered for segments in _CREDENTIAL_SEGMENTS):
        return path
    parts = path.split("/")
    names = [_segment_name(part) for part in parts]
    named = [i for i, name in enumerate(names) if name not in _EMPTY_SEGMENTS]
    ends = [_prefix_end(names, named, segments) for segments in _CREDENTIAL_SEGMENTS]
    found = [end for end in ends if end is not None]
    if not found:
        return path
    end = min(found)
    head = "/".join(parts[:end] + [parts[end][: _name_length(parts[end])]])
    if not path[len(head) :].strip("/"):
        return path
    return f"{head}/{REDACTED}"


def redact_target(target: str) -> str:
    """A request target, its path and any query, as a log line may print it.

    The query string is replaced whole by `REDACTED`: the OAuth callbacks'
    ``code`` and ``state`` and the Meta deletion-status ``code`` arrive there,
    and nothing a query carries is worth a secret in the log. The ``?`` stays,
    so the line still shows that a query came. The path goes through
    `redact_path`.
    """
    path, question, _query = target.partition("?")
    return redact_path(path) + (f"?{REDACTED}" if question else "")


def loggable_path(scope: Mapping[str, Any]) -> str:
    """The path an application log line names for a request.

    For a routed request, the route's template with each `LOGGABLE_PATH_PARAMS`
    value put back (``/api/v1/workspaces/<ws-id>/commands/approve``); every
    other parameter keeps its ``{name}`` (``/api/v1/invitations/{token}/accept``).
    Before routing, or when no route matched, the raw path through
    `redact_path`. Either way what the caller sent is percent-encoded, as
    uvicorn's access line prints a path, so a newline in it cannot start a
    forged line. It takes the ASGI scope, so a middleware, which has no
    `Request`, calls it as a handler does (``loggable_path(request.scope)``).
    """
    template = getattr(scope.get("route"), "path", None)
    if isinstance(template, str):
        params = scope.get("path_params") or {}

        def value(match: re.Match) -> str:
            name = match.group(1)
            if name in LOGGABLE_PATH_PARAMS and name in params:
                return quote(str(params[name]), safe="")
            return match.group(0)

        return _PATH_PARAM.sub(value, template)
    return redact_path(quote(str(scope.get("path") or "")))


def _redact_scope(scope: Mapping[str, Any]) -> dict:
    """A copy of an ASGI scope, as uvicorn's trace line prints it, with its
    target masked: ``path`` through `redact_path`, and any ``raw_path`` and
    ``query_string`` as `REDACTED`. ``raw_path`` is the target as sent, still
    percent-encoded, so a prefix can hide in it (``invitation%73``) while the
    request still reaches its route; the masked ``path`` names the request
    instead. The scope the app runs on is untouched."""
    shown = dict(scope)
    shown["path"] = redact_path(str(scope.get("path") or ""))
    if scope.get("raw_path"):
        shown["raw_path"] = REDACTED
    if scope.get("query_string"):
        shown["query_string"] = REDACTED
    return shown


def _redact_argument(arg: Any) -> Any:
    if isinstance(arg, str):
        return redact_target(arg)
    if isinstance(arg, dict) and "path" in arg:
        return _redact_scope(arg)
    return arg


class AccessLogRedactor(logging.Filter):
    """uvicorn's request lines, with no query string and their paths masked.

    On ``uvicorn.access`` uvicorn logs each response as
    ``'%s - "%s %s HTTP/%s" %d'`` over ``(client_addr, method, full_path,
    http_version, status_code)``, and its ``AccessFormatter`` unpacks exactly
    those five arguments (uvicorn 0.47.0: ``protocols/http/h11_impl.py``,
    ``logging.py``). ``full_path`` is the path plus the raw query string. On
    ``uvicorn.asgi``, at trace level only, ``MessageLoggerMiddleware`` logs
    each request's scope as a dict (``middleware/message_logger.py``).

    Every string argument goes through `redact_target`, every dict that
    carries a ``path`` through `_redact_scope`, and every other argument is
    kept, so the tuple keeps its length, order and types and the formatter
    unpacks it as before. On the access line only the path can change: the
    client address is ``host:port``, the method an HTTP token and the version
    digits.

    It never drops a record and never raises: a logger's filter runs inside
    uvicorn's ``send``, so an exception here would fail the response.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(_redact_argument(arg) for arg in record.args)
        return True


def install_access_log_redaction() -> None:
    """Put one `AccessLogRedactor` on each of `REDACTED_LOGGERS`.

    Run at import, below. uvicorn configures its loggers when it builds its
    ``Config`` and only then imports the app, whose import brings this module
    in (`src/api/app.py`), so the filter is in place before the first request.
    ``logging.config.dictConfig`` replaces a logger's handlers and keeps its
    filters, so a later reconfiguration keeps it too.
    """
    for name in REDACTED_LOGGERS:
        target = logging.getLogger(name)
        if not any(isinstance(f, AccessLogRedactor) for f in target.filters):
            target.addFilter(AccessLogRedactor())


install_access_log_redaction()
