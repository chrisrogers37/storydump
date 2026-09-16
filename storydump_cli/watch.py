"""``--watch``: re-read a view on an interval and print only what changed.

A read is the same list the verb prints — ``[{"workspace_id", "rows"}]`` —
and rows are keyed by the view's id column (``WATCHED``), so a key that is
new is ``added``, a row that came back different is ``changed``, and a
key that has gone is ``removed`` (a story stepping out of ``floating`` is
the change worth seeing). The first read has no baseline, so it prints
every row as added: the watch opens on the state it found.

Each verb names its terminal condition — the watch ends with 0 — and its
failure condition, a ``Failure`` with the watch exit code that ``main``
renders as one error envelope after the read that showed it. A verb with
neither runs until Ctrl-C, which is also 0: stopping a watch is not an
error. An API that stops answering is the same answer it is anywhere
else, through the mapping ``main`` already owns. The clock and the
sleeper come from the runtime so a test runs a scripted sequence
instantly.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any, Callable, Hashable, Mapping, Optional

from src.services.target.vocabulary import EXIT_OK, EXIT_WATCH_FAILED, envelope
from storydump_cli.output import Failure, emit, redact

Row = dict[str, Any]
#: What one read returns: ``[{"workspace_id": …, "rows": [Row, …]}, …]``.
Reading = list[dict[str, Any]]
KeyFn = Callable[[Row], Hashable]

DEFAULT_EVERY = 30.0
WATCH_FIX = (
    "the failing rows are in the last change printed;"
    " storydump story <id> has a story's timeline"
)


@dataclass(frozen=True)
class Watched:
    """How one verb is watched: its row key, and its two conditions."""

    kind: str
    key: KeyFn
    #: A sentence when the read's rows show the failure condition, else None.
    failed: Callable[[list[Row]], Optional[str]]
    #: Whether the watch has reached its end, given the previous read's rows
    #: (None on the first read) and the current ones.
    done: Callable[[Optional[list[Row]], list[Row]], bool]
    #: The key of a reading's entries (a workspace's id for the views, the
    #: service's name for `deploys`) and the name of the list the JSON watch
    #: envelope carries them under.
    scope: str = "workspace_id"
    collection: str = "workspaces"
    #: Whether the FIRST read is judged too: a view's baseline is what was
    #: already there (printed, not fatal); a deploy that has already failed
    #: is the answer.
    failed_on_baseline: bool = False


# --- keys ---------------------------------------------------------------------


def _by_id(row: Row) -> Hashable:
    return row.get("id")


def _jobs_key(row: Row) -> Hashable:
    return (row.get("kind"), row.get("lane"), row.get("state"))


def _outbox_key(row: Row) -> Hashable:
    return (row.get("binding_id"), row.get("kind"), row.get("state"))


def _burst_key(row: Row) -> Hashable:
    """A burst row's identity: its section, moment and story — plus the
    permit's generation (two permits of one story share a transaction's
    ``now()``), the sibling's waiter (one post past two waiters is two
    rows) and the census row's state (no time, no story)."""
    return (
        row.get("section"),
        row.get("at"),
        row.get("intent_id"),
        row.get("generation"),
        row.get("waiting_id"),
        row.get("state") if row.get("section") == "outcome" else None,
    )


def _story_key(row: Row) -> Hashable:
    intent = row.get("intent")
    return intent.get("id") if isinstance(intent, dict) else None


# --- conditions ---------------------------------------------------------------


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _failed_floating(rows: list[Row]) -> Optional[str]:
    n = sum(1 for row in rows if row.get("job_state") == "failed")
    return (
        f"{_count(n, 'floating story', 'floating stories')} whose job has failed"
        if n
        else None
    )


def _failed_burst(rows: list[Row]) -> Optional[str]:
    n = sum(1 for row in rows if row.get("section") == "review")
    return (
        f"{_count(n, 'story', 'stories')} sent to review during the burst"
        if n
        else None
    )


def _failed_jobs(rows: list[Row]) -> Optional[str]:
    n = sum(int(row.get("count") or 0) for row in rows if row.get("state") == "failed")
    return f"{_count(n, 'failed job', 'failed jobs')}" if n else None


def _failed_outbox(rows: list[Row]) -> Optional[str]:
    n = sum(int(row.get("count") or 0) for row in rows if row.get("state") == "failed")
    return f"{_count(n, 'failed outbox row', 'failed outbox rows')}" if n else None


def _never_fails(rows: list[Row]) -> Optional[str]:
    return None


def _empty_twice(previous: Optional[list[Row]], current: list[Row]) -> bool:
    return previous is not None and not previous and not current


def mid_flight(row: Row) -> bool:
    """A burst row that says a story is still on its way: a container
    permitted and not yet published, or the census counting a story in
    ``publishing``."""
    section = row.get("section")
    if section == "permit":
        return row.get("state") == "permitted"
    if section == "outcome":
        return row.get("state") == "publishing"
    return False


def _nothing_mid_flight(previous: Optional[list[Row]], current: list[Row]) -> bool:
    return not any(mid_flight(row) for row in current)


def _never_done(previous: Optional[list[Row]], current: list[Row]) -> bool:
    return False


WATCHED: Mapping[str, Watched] = {
    "story": Watched("story", _story_key, _never_fails, _never_done),
    "cards": Watched("cards", _by_id, _never_fails, _never_done),
    "floating": Watched("floating", _by_id, _failed_floating, _empty_twice),
    "account": Watched("account", _by_id, _never_fails, _never_done),
    "jobs": Watched("jobs", _jobs_key, _failed_jobs, _never_done),
    "outbox": Watched("outbox", _outbox_key, _failed_outbox, _never_done),
    "burst": Watched("burst", _burst_key, _failed_burst, _nothing_mid_flight),
}


# --- the diff -------------------------------------------------------------------


def _keyed(
    reading: Reading, key: KeyFn, scope: str = "workspace_id"
) -> dict[str, dict[Hashable, Row]]:
    keyed: dict[str, dict[Hashable, Row]] = {}
    for entry in reading:
        rows = keyed.setdefault(str(entry.get(scope)), {})
        for row in entry.get("rows") or []:
            if isinstance(row, dict):
                rows[key(row)] = row
    return keyed


def diff(
    previous: Optional[Reading],
    current: Reading,
    key: KeyFn,
    *,
    scope: str = "workspace_id",
) -> list[dict[str, Any]]:
    """``[{<scope>, "changes": [{"change", "row"}]}]`` — every entry of the
    current read in its order (added and changed rows in the read's order,
    then the rows that have gone), then any entry that has gone with all its
    rows removed. *scope* is the entry's key: a workspace's id for the views."""
    before = {} if previous is None else _keyed(previous, key, scope)
    after = _keyed(current, key, scope)
    out: list[dict[str, Any]] = []
    for workspace_id, rows in after.items():
        old = before.get(workspace_id, {})
        changes: list[dict[str, Any]] = []
        for k, row in rows.items():
            if k not in old:
                changes.append({"change": "added", "row": row})
            elif old[k] != row:
                changes.append({"change": "changed", "row": row})
        for k, row in old.items():
            if k not in rows:
                changes.append({"change": "removed", "row": row})
        out.append({scope: workspace_id, "changes": changes})
    for workspace_id, rows in before.items():
        if workspace_id not in after:
            out.append(
                {
                    scope: workspace_id,
                    "changes": [
                        {"change": "removed", "row": row} for row in rows.values()
                    ],
                }
            )
    return out


def _flatten(reading: Reading) -> list[Row]:
    return [
        row
        for entry in reading
        for row in entry.get("rows") or []
        if isinstance(row, dict)
    ]


# --- printing ---------------------------------------------------------------------


def _value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"), sort_keys=True)
    return str(value)


def summarize(kind: str, row: Row) -> str:
    """One line of ``field=value`` pairs: the row itself, or for a story its
    intent with the size of each of its lists."""
    source = row
    if kind == "story" and isinstance(row.get("intent"), dict):
        source = row["intent"]
    parts = [f"{k}={_value(v)}" for k, v in source.items() if k != "workspace_id"]
    if kind == "story":
        parts += [
            f"{name}={len(row.get(name) or [])}"
            for name in ("audit", "operations", "cards")
        ]
    return " ".join(parts)


def _print(runtime: Any, watched: Watched, changes: list[dict[str, Any]]) -> None:
    if runtime.json_mode:
        emit(envelope(watched.kind, {watched.collection: changes}), json_mode=True)
        return
    stamp = runtime.now_fn().strftime("%H:%M:%S")
    for entry in changes:
        for change in entry["changes"]:
            line = (
                f"{stamp}  {change['change']:<7}  {entry[watched.scope]}"
                f"  {summarize(watched.kind, change['row'])}"
            )
            sys.stdout.write(redact(line) + "\n")
    sys.stdout.flush()


# --- the loop ---------------------------------------------------------------------


def watch(
    runtime: Any, watched: Watched, read: Callable[[], Reading], *, every: float
) -> int:
    """Read, print the changes, judge the read, sleep; the exit code."""
    previous: Optional[Reading] = None
    try:
        while True:
            current = read()
            changes = diff(previous, current, watched.key, scope=watched.scope)
            _print(runtime, watched, changes)
            rows = _flatten(current)
            # the first read is the baseline: what was already failing is
            # printed, not fatal — the watch exits 6 on a failure that
            # ARRIVES (a row added or changed), so a window that never slides
            # can still be waited on
            fresh = [
                change["row"]
                for entry in changes
                for change in entry["changes"]
                if change["change"] in ("added", "changed")
            ]
            if previous is None and watched.failed_on_baseline:
                failure = watched.failed(rows)
            else:
                failure = watched.failed(fresh) if previous is not None else None
            if failure is not None:
                raise Failure(
                    code=EXIT_WATCH_FAILED,
                    reason="watch_failed",
                    detail=failure,
                    fix=WATCH_FIX,
                )
            if watched.done(None if previous is None else _flatten(previous), rows):
                return EXIT_OK
            previous = current
            runtime.sleep_fn(every)
    except KeyboardInterrupt:
        return EXIT_OK
