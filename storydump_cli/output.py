"""What the CLI prints, and the two rules every line obeys.

One: the document is the envelope (``vocabulary.envelope`` /
``error_envelope``) whether it is printed as JSON or rendered for a person —
the tests run ``check_envelope`` over everything emitted, so the two modes
cannot drift apart. Two: no secret reaches a terminal or a log. A token, a
database URL or a webhook secret is redacted before printing — inside the
document for JSON, so the output stays valid JSON, and in every string a
renderer is handed for a person. Rendering is per kind; a kind without a
renderer prints its data as indented JSON rather than nothing.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any, Callable, Mapping

from rich.console import Console
from rich.table import Table

#: The three secret shapes the spec names (§6, redaction at the client).
TOKEN_PATTERN = re.compile(r"sdt_[A-Za-z0-9_-]{8,}")
DATABASE_URL_PATTERN = re.compile(r"postgres(?:ql)?://\S+")
WEBHOOK_SECRET_PATTERN = re.compile(r"(secret_token|token)=\S+")

#: Wide enough that a table of ids never folds when the output is a pipe or
#: a test; a real terminal keeps its own width.
PIPE_WIDTH = 200


def redact(text: str) -> str:
    """*text* with every token, database URL and webhook secret replaced."""
    text = TOKEN_PATTERN.sub("sdt_…", text)
    text = DATABASE_URL_PATTERN.sub("postgres://<redacted>", text)
    text = WEBHOOK_SECRET_PATTERN.sub(r"\1=<redacted>", text)
    return text


def redact_document(value: Any) -> Any:
    """*value* with every string leaf (and key) redacted, structure intact."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {
            redact(key) if isinstance(key, str) else key: redact_document(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_document(item) for item in value]
    return value


class Failure(Exception):
    """A verb's own answer that is not success — an unknown token id, no
    token stored: the four fields of an error envelope, which ``main``
    renders under the verb's kind and turns into the exit code."""

    def __init__(self, *, code: int, reason: str, detail: str, fix: str) -> None:
        super().__init__(detail)
        self.code = code
        self.reason = reason
        self.detail = detail
        self.fix = fix


def emit(document: Mapping[str, Any], *, json_mode: bool) -> None:
    """Print one envelope: a single JSON line on stdout in JSON mode (errors
    included — an agent reads one stream); for a person, the data rendered
    on stdout, or ``error:``/``fix:`` on stderr."""
    if json_mode:
        sys.stdout.write(json.dumps(redact_document(document)) + "\n")
        sys.stdout.flush()
        return
    error = document.get("error")
    if error is not None:
        sys.stderr.write(redact(f"error: {error['detail']}\nfix: {error['fix']}\n"))
        sys.stderr.flush()
        return
    renderer = RENDERERS.get(str(document.get("kind")), _render_generic)
    renderer(_console(), redact_document(document.get("data")))


def _console() -> Console:
    stream = sys.stdout
    is_tty = bool(getattr(stream, "isatty", None) and stream.isatty())
    return Console(
        file=stream,
        width=None if is_tty else PIPE_WIDTH,
        force_terminal=None if is_tty else False,
        markup=False,
        highlight=False,
        emoji=False,
    )


def _table(*columns: str) -> Table:
    table = Table(box=None, pad_edge=False, header_style="bold")
    for column in columns:
        table.add_column(column)
    return table


def _text(value: Any, absent: str) -> str:
    return absent if value is None or value == "" else str(value)


def _principal_parts(data: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    principal = data if isinstance(data, dict) else {}
    token = principal.get("token") or {}
    workspaces = [
        ws for ws in principal.get("workspaces") or [] if isinstance(ws, dict)
    ]
    return (token if isinstance(token, dict) else {}), workspaces


def _render_login(console: Console, data: Any) -> None:
    token, workspaces = _principal_parts(data)
    names = ", ".join(_text(ws.get("name") or ws.get("id"), "?") for ws in workspaces)
    console.print(
        f"signed in as {_text(token.get('name'), '?')} ({_text(token.get('role'), '?')})"
        f" — workspaces: {names or 'none'}"
    )


def _render_whoami(console: Console, data: Any) -> None:
    token, workspaces = _principal_parts(data)
    principal = data if isinstance(data, dict) else {}
    console.print(f"kind       {_text(principal.get('kind'), '?')}")
    if token:
        console.print(
            f"token      {_text(token.get('name'), '?')} ({_text(token.get('role'), '?')}),"
            f" expires {_text(token.get('expires_at'), 'never')}"
        )
        if token.get("workspace_id"):
            console.print(f"workspace  {token['workspace_id']}  (service identity)")
    if principal.get("user_id"):
        console.print(f"user       {principal['user_id']}")
    console.print("workspaces")
    if not workspaces:
        console.print("  none")
        return
    table = _table("  name", "role", "id")
    for ws in workspaces:
        table.add_row(
            "  " + _text(ws.get("name"), "?"),
            _text(ws.get("role"), "?"),
            _text(ws.get("id"), "?"),
        )
    console.print(table)


def _render_tokens(console: Console, data: Any) -> None:
    rows = data.get("tokens") if isinstance(data, dict) else None
    if isinstance(data, dict) and "revoked" in data:
        revoked = data["revoked"] if isinstance(data["revoked"], dict) else {}
        console.print(f"revoked {_text(revoked.get('name') or revoked.get('id'), '?')}")
        return
    if not rows:
        console.print("no tokens")
        return
    table = _table("id", "name", "role", "expires", "last used", "revoked")
    for row in rows:
        table.add_row(
            _text(row.get("id"), "?"),
            _text(row.get("name"), "?"),
            _text(row.get("role"), "?"),
            _text(row.get("expires_at"), "never"),
            _text(row.get("last_used_at"), "never"),
            _text(row.get("revoked_at"), "no"),
        )
    console.print(table)


def _render_logout(console: Console, data: Any) -> None:
    console.print("signed out")
    note = data.get("note") if isinstance(data, dict) else None
    if note:
        console.print(f"note: {note}")


def _render_generic(console: Console, data: Any) -> None:
    console.print(json.dumps(data, indent=2, sort_keys=True))


RENDERERS: Mapping[str, Callable[[Console, Any], None]] = {
    "login": _render_login,
    "whoami": _render_whoami,
    "tokens": _render_tokens,
    "logout": _render_logout,
}
