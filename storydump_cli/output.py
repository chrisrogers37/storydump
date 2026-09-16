"""What the CLI prints, and the two rules every line obeys.

One: the document is the envelope (``vocabulary.envelope`` /
``error_envelope``) whether it is printed as JSON or rendered for a person —
the tests run ``check_envelope`` over everything emitted, so the two modes
cannot drift apart. Two: no secret reaches a terminal or a log. A token, a
database URL or a webhook secret is redacted before printing — inside the
document for JSON, so the output stays valid JSON, and in every string a
renderer is handed for a person. Rendering is per kind; a kind without a
renderer prints its data as indented JSON rather than nothing.

The read views share one data shape — ``{"workspaces": [{"workspace_id",
"rows"}]}`` — and one frame for a person: a ``workspace <id>`` line, then
that workspace's table (``story`` and ``burst`` in sections), or the view's
own word for nothing. Id columns never wrap: an id folded over two lines
cannot be copied, so when a table is wider than the terminal the other
columns give.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any, Callable, Mapping, Sequence

from rich.console import Console
from rich.padding import Padding
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


def _handle(value: object) -> str:
    """An Instagram handle with exactly one leading @, whatever the ledger stored."""
    text = _text(value, "?")
    return text if text.startswith("@") else "@" + text


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


# --- the read views (phase 02) ------------------------------------------------

#: A column: its header, and the row field it shows or a function of the row.
Column = tuple[str, Any]

#: Fields that are ids: never folded, whatever the width.
ID_FIELDS = frozenset(
    {
        "id",
        "intent_id",
        "binding_id",
        "posted_id",
        "waiting_id",
        "ig_account_id",
        "media_item_id",
        "actor_user_id",
    }
)

#: What a workspace with no rows says, per view.
EMPTY: Mapping[str, str] = {
    "story": "no such story here",
    "cards": "nothing sent for it here",
    "floating": "nothing floating",
    "account": "no such account here",
    "jobs": "no jobs",
    "outbox": "outbox empty",
    "burst": "nothing in the window",
}

STORY_FIELDS: Sequence[Column] = (
    ("state", "state"),
    ("step", "publish_step"),
    ("since", "entered_state_at"),
    ("slot", "schedule_slot_at"),
    ("cap", "cap_consumed_on"),
    ("attempts", "attempts_by_step"),
    ("account", "ig_account_id"),
    ("media", "media_item_id"),
    ("error", "last_error"),
)
AUDIT_COLUMNS: Sequence[Column] = (
    ("at", "at"),
    ("from", "from_state"),
    ("to", "to_state"),
    ("actor", "actor_kind"),
    ("user", "actor_user_id"),
    ("channel", "channel"),
    ("detail", "detail"),
)
OPERATION_COLUMNS: Sequence[Column] = (
    ("at", "at"),
    ("kind", "op_kind"),
    ("gen", "generation"),
    ("state", "state"),
    ("variant", "url_variant"),
    ("error", "error"),
    ("subcode", "subcode"),
    ("ms", "elapsed_ms"),
)
SENT_COLUMNS: Sequence[Column] = (
    ("at", "at"),
    ("binding", "binding_id"),
    ("kind", "kind"),
    ("state", "state"),
    ("message ref", "external_message_ref"),
    ("attempts", "attempts"),
    ("outcome", "outcome_text"),
)
CARDS_COLUMNS: Sequence[Column] = (
    ("id", "id"),
    ("created", "created_at"),
    ("binding", "binding_id"),
    ("channel", "channel"),
    ("kind", "kind"),
    ("state", "state"),
    ("message ref", "external_message_ref"),
    ("attempts", "attempts"),
    ("outcome", "outcome_text"),
    ("updated", "updated_at"),
)


def _job_of(row: Mapping[str, Any]) -> Any:
    """``ready (2)``: the job's state and its attempts."""
    state = row.get("job_state")
    if state is None:
        return None
    attempts = row.get("job_attempts")
    return f"{state} ({attempts})" if attempts is not None else str(state)


def _wait_of(row: Mapping[str, Any]) -> Any:
    """``container_not_ready/2``: the last wait's class and rung."""
    klass = row.get("last_wait_class")
    if klass is None:
        return None
    rung = row.get("last_wait_rung")
    return f"{klass}/{rung}" if rung is not None else str(klass)


FLOATING_COLUMNS: Sequence[Column] = (
    ("id", "id"),
    ("step", "publish_step"),
    ("attempts", "attempts_by_step"),
    ("cap", "cap_consumed_on"),
    ("since", "entered_state_at"),
    ("job", _job_of),
    ("run at", "job_run_at"),
    ("wait", _wait_of),
    ("waited at", "last_wait_at"),
)
RECENT_COLUMNS: Sequence[Column] = (
    ("id", "id"),
    ("state", "state"),
    ("since", "entered_state_at"),
)
JOBS_COLUMNS: Sequence[Column] = (
    ("kind", "kind"),
    ("lane", "lane"),
    ("state", "state"),
    ("count", "count"),
    ("oldest run at", "oldest_run_at"),
)
SAMPLE_COLUMNS: Sequence[Column] = (
    ("id", "id"),
    ("attempts", "attempts"),
    ("run at", "run_at"),
    ("error", "error"),
)
OUTBOX_COLUMNS: Sequence[Column] = (
    ("binding", "binding_id"),
    ("channel", "channel"),
    ("ref", "external_ref"),
    ("kind", "kind"),
    ("state", "state"),
    ("count", "count"),
    ("oldest", "oldest_created_at"),
)
#: The burst's sections in the order they read, each with its columns.
BURST_SECTIONS: Sequence[tuple[str, Sequence[Column]]] = (
    (
        "tap",
        (
            ("at", "at"),
            ("story", "intent_id"),
            ("from", "from_state"),
            ("to", "to_state"),
            ("actor", "actor_kind"),
            ("channel", "channel"),
        ),
    ),
    (
        "permit",
        (
            ("at", "at"),
            ("story", "intent_id"),
            ("gen", "generation"),
            ("state", "state"),
            ("variant", "url_variant"),
            ("error", "error"),
            ("subcode", "subcode"),
            ("ms", "elapsed_ms"),
        ),
    ),
    (
        "float_wait",
        (
            ("at", "at"),
            ("story", "intent_id"),
            ("class", "wait_class"),
            ("rung", "rung"),
            ("seconds", "seconds"),
            ("next run", "next_run_at"),
        ),
    ),
    (
        "sibling",
        (
            ("at", "at"),
            ("posted", "posted_id"),
            ("waiting", "waiting_id"),
            ("class", "wait_class"),
        ),
    ),
    (
        "review",
        (
            ("at", "at"),
            ("story", "intent_id"),
            ("from", "from_state"),
            ("last error", "last_error"),
        ),
    ),
    ("outcome", (("state", "state"), ("count", "count"))),
)
MIGRATION_COLUMNS: Sequence[Column] = (
    ("version", "version"),
    ("status", "status"),
    ("applied at", "applied_at"),
    ("checksum", "checksum"),
)
RLS_COLUMNS: Sequence[Column] = (
    ("table", "table"),
    ("enabled", "enabled"),
    ("forced", "forced"),
)
DOOR_COLUMNS: Sequence[Column] = (("name", "name"), ("owner", "owner"))


def _cell(value: Any) -> str:
    """One cell: ``-`` for nothing, ``yes``/``no`` for a flag, ``k=v`` pairs
    for a mapping, compact JSON for a list."""
    if value is None or value == "":
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, dict):
        return " ".join(f"{k}={_cell(v)}" for k, v in value.items()) or "-"
    if isinstance(value, list):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


def _rows_table(rows: Sequence[Mapping[str, Any]], columns: Sequence[Column]) -> Table:
    """A table that never crops: an id column keeps its width, every other
    column folds when the terminal is narrower than the table."""
    table = Table(box=None, pad_edge=False, header_style="bold")
    for header, field in columns:
        if isinstance(field, str) and field in ID_FIELDS:
            table.add_column(header, no_wrap=True)
        else:
            table.add_column(header, overflow="fold")
    for row in rows:
        table.add_row(
            *(
                _cell(field(row) if callable(field) else row.get(field))
                for _, field in columns
            )
        )
    return table


def _indented(table: Table, indent: int) -> Padding:
    return Padding(table, (0, 0, 0, indent), expand=False)


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [item for item in (value or []) if isinstance(item, dict)]


def _section(
    console: Console, title: str, rows: Any, columns: Sequence[Column], indent: int = 2
) -> None:
    console.print(" " * indent + title)
    items = _dicts(rows)
    if not items:
        console.print(" " * (indent + 2) + "none")
        return
    console.print(_indented(_rows_table(items, columns), indent + 2))


def _entries(data: Any) -> list[tuple[str, list[dict[str, Any]]]]:
    workspaces = data.get("workspaces") if isinstance(data, dict) else None
    return [
        (_text(entry.get("workspace_id"), "?"), _dicts(entry.get("rows")))
        for entry in _dicts(workspaces)
    ]


def _view(
    kind: str, render_rows: Callable[[Console, list[dict[str, Any]]], None]
) -> Callable[[Console, Any], None]:
    """The frame every workspace view shares: a header per workspace, then
    its rows or the view's word for none."""

    def render(console: Console, data: Any) -> None:
        entries = _entries(data)
        if not entries:
            console.print("no workspaces to read")
            return
        for index, (workspace_id, rows) in enumerate(entries):
            if index:
                console.print()
            console.print(f"workspace {workspace_id}")
            if not rows:
                console.print(f"  {EMPTY[kind]}")
                continue
            render_rows(console, rows)

    return render


def _table_of(
    columns: Sequence[Column],
) -> Callable[[Console, list[dict[str, Any]]], None]:
    def render_rows(console: Console, rows: list[dict[str, Any]]) -> None:
        console.print(_indented(_rows_table(rows, columns), 2))

    return render_rows


def _render_story_rows(console: Console, rows: list[dict[str, Any]]) -> None:
    for row in rows:
        intent = row.get("intent") if isinstance(row.get("intent"), dict) else {}
        console.print(f"  story {_text(intent.get('id'), '?')}")
        for label, field in STORY_FIELDS:
            console.print(f"    {label:<9} {_cell(intent.get(field))}")
        _section(console, "audit", row.get("audit"), AUDIT_COLUMNS)
        _section(console, "operations", row.get("operations"), OPERATION_COLUMNS)
        _section(console, "outbox", row.get("cards"), SENT_COLUMNS)


def _render_account_rows(console: Console, rows: list[dict[str, Any]]) -> None:
    for row in rows:
        today = row.get("today") if isinstance(row.get("today"), dict) else None
        used = (
            f"{_cell(today.get('count'))}/{_cell(today.get('cap_at_write'))}"
            f" on {_cell(today.get('local_date'))}"
            if today
            else "nothing yet"
        )
        console.print(f"  {_handle(row.get('handle'))}  {_text(row.get('id'), '?')}")
        console.print(f"    cap/day   {_cell(row.get('posts_per_day'))}")
        console.print(f"    today     {used}")
        console.print(f"    tz        {_cell(row.get('tz'))}")
        console.print(f"    next slot {_cell(row.get('next_slot_at'))}")
        _section(console, "recent", row.get("recent"), RECENT_COLUMNS, indent=4)


def _render_jobs_rows(console: Console, rows: list[dict[str, Any]]) -> None:
    console.print(_indented(_rows_table(rows, JOBS_COLUMNS), 2))
    for row in rows:
        samples = _dicts(row.get("samples"))
        if samples:
            title = f"{_cell(row.get('kind'))}/{_cell(row.get('lane'))} {_cell(row.get('state'))}"
            _section(console, title, samples, SAMPLE_COLUMNS)


def _render_burst_rows(console: Console, rows: list[dict[str, Any]]) -> None:
    known = {section for section, _ in BURST_SECTIONS}
    for section, columns in BURST_SECTIONS:
        matching = [row for row in rows if row.get("section") == section]
        if matching:
            _section(console, section, matching, columns)
    for row in rows:
        if row.get("section") not in known:
            console.print("  " + json.dumps(row, sort_keys=True))


def _render_posture(console: Console, data: Any) -> None:
    posture = data if isinstance(data, dict) else {}
    role = posture.get("role") if isinstance(posture.get("role"), dict) else {}
    console.print(
        f"role {_text(role.get('user'), '?')}  bypassrls {_cell(role.get('bypassrls'))}"
    )
    # present | absent | unreadable — the F7 grant signal, not the same as an
    # empty list of migrations
    console.print(f"ledger {_text(posture.get('ledger'), '?')}")
    _section(
        console, "migrations", posture.get("migrations"), MIGRATION_COLUMNS, indent=0
    )
    _section(console, "rls", posture.get("rls"), RLS_COLUMNS, indent=0)
    _section(console, "doors", posture.get("doors"), DOOR_COLUMNS, indent=0)


RENDERERS: Mapping[str, Callable[[Console, Any], None]] = {
    "login": _render_login,
    "whoami": _render_whoami,
    "tokens": _render_tokens,
    "logout": _render_logout,
    "story": _view("story", _render_story_rows),
    "cards": _view("cards", _table_of(CARDS_COLUMNS)),
    "floating": _view("floating", _table_of(FLOATING_COLUMNS)),
    "account": _view("account", _render_account_rows),
    "jobs": _view("jobs", _render_jobs_rows),
    "outbox": _view("outbox", _table_of(OUTBOX_COLUMNS)),
    "burst": _view("burst", _render_burst_rows),
    "posture": _render_posture,
}
