"""The read views: ``story``, ``cards``, ``floating``, ``account``, ``jobs``,
``outbox``, ``burst`` and ``posture``.

Every view answers for ONE workspace — the API's envelope carries that
workspace's rows — and the CLI does the looping. ``--workspace`` names one:
an id goes to the API as it is (the API is the authority on membership, and
a workspace the principal cannot see is its 404), a name is resolved through
``/me/principal`` and one that matches nothing is "no such workspace".
Without it, every workspace the principal lists is read in turn. Whatever
was read, ``data`` has one shape — ``{"workspaces": [{"workspace_id",
"rows"}]}`` — so an agent parses one document whether the token sees one
workspace or ten. ``posture`` is the exception: not a workspace's rows but
the deployment's own, so its ``data`` is the view's object.

A key (``story``, ``cards``, ``account``) that resolves to nothing in every
workspace is an answer, not a traceback: exit 1 with the CLI's sentence.
``--since`` is a span back from now (``15m``, ``3h``, ``1d``) or an
ISO-8601 timestamp, and reaches the API as ISO-8601 UTC either way.
``--watch`` hands the same read to ``storydump_cli.watch``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import click

from src.services.target.vocabulary import (
    DEFAULT_WINDOW,
    EXIT_NOT_FOUND,
    envelope,
    window_start,
)
from storydump_cli.client import Client, Unreachable
from storydump_cli.commands import begin, global_options
from storydump_cli.commands.auth import signed_in_client
from storydump_cli.output import Failure, emit
from storydump_cli.watch import DEFAULT_EVERY, WATCHED, Reading, watch

DEFAULT_SINCE = DEFAULT_WINDOW
WORKSPACES_FIX = "run storydump whoami for the workspaces this token can see"


# --- --since ------------------------------------------------------------------


def parse_since(value: str, now: datetime) -> datetime:
    """The vocabulary's one window grammar (`window_start`): a span back from
    *now* or an ISO-8601 timestamp, at most thirty days, never in the future;
    always an aware UTC datetime. Anything else is a usage error with the
    grammar's own sentence."""
    try:
        return window_start(value, now)
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--since") from None


def iso_utc(moment: datetime) -> str:
    """``2026-09-15T12:00:00Z`` — what the API's ``since`` is given."""
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _since(ctx: click.Context, runtime: Any, value: str) -> str:
    try:
        return iso_utc(parse_since(value, runtime.now_fn()))
    except click.BadParameter as exc:
        exc.ctx = ctx
        raise


# --- the workspaces to read ---------------------------------------------------


def _as_uuid(value: str) -> Optional[str]:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def _uuid_argument(ctx: click.Context, param: click.Parameter, value: str) -> str:
    """A story id is a UUID; the probes' eight-character habit is a usage
    error here, not a 422 from the API."""
    if _as_uuid(value) is None:
        raise click.BadParameter("a story id is a full UUID", ctx=ctx, param=param)
    return str(uuid.UUID(value))


def _targets(client: Client, workspace: Optional[str]) -> list[str]:
    """The workspace ids to read: the one named (an id as given, a name
    through the principal), or every one the principal lists."""
    if workspace is not None:
        as_id = _as_uuid(workspace)
        if as_id is not None:
            return [as_id]
    principal = client.principal()
    listed = [
        ws
        for ws in (principal.get("workspaces") if isinstance(principal, dict) else [])
        or []
        if isinstance(ws, dict) and ws.get("id")
    ]
    if workspace is None:
        return [str(ws["id"]) for ws in listed]
    # every workspace of that name — two studios called "Studio" are both read
    named = [str(ws["id"]) for ws in listed if ws.get("name") == workspace]
    if named:
        return named
    raise Failure(
        code=EXIT_NOT_FOUND,
        reason="not_found",
        detail=f"no such workspace: {workspace}",
        fix=f"{WORKSPACES_FIX} (an id works too)",
    )


def _unwrap(answer: Any, workspace_id: str) -> dict[str, Any]:
    """The rows of one workspace out of the API's envelope."""
    data = answer.get("data") if isinstance(answer, dict) else None
    rows = data.get("rows") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise Unreachable(200, None, "the answer was not the view's envelope")
    ws = data.get("workspace_id")
    return {
        "workspace_id": ws if isinstance(ws, str) and ws else workspace_id,
        "rows": rows,
    }


def _run_view(
    ctx: click.Context,
    kind: str,
    call: Callable[[Client, str], Any],
    *,
    workspace: Optional[str],
    watch_mode: bool,
    every: Optional[float],
    missing: Optional[str] = None,
) -> Optional[int]:
    """One read verb: resolve the workspaces, read each, answer once — or
    hand the read to the watch. *missing* is the sentence for a key that
    resolved to nothing anywhere (exit 1); None for a view with no key."""
    runtime = begin(ctx, kind)
    if every is not None and not watch_mode:
        raise click.UsageError("--every only means something with --watch", ctx=ctx)
    client = signed_in_client(runtime)
    targets = _targets(client, workspace)

    def read() -> Reading:
        return [_unwrap(call(client, target), target) for target in targets]

    if watch_mode:
        return watch(
            runtime,
            WATCHED[kind],
            read,
            every=DEFAULT_EVERY if every is None else every,
        )
    reading = read()
    if missing is not None and not any(entry["rows"] for entry in reading):
        raise Failure(
            code=EXIT_NOT_FOUND,
            reason="not_found",
            detail=missing,
            fix=f"check the id or handle, and {WORKSPACES_FIX}",
        )
    emit(envelope(kind, {"workspaces": reading}), json_mode=runtime.json_mode)
    return None


# --- the options every view shares ----------------------------------------------


def view_options(command: Callable[..., Any]) -> Callable[..., Any]:
    """``--workspace``, ``--watch``, ``--every`` and the global two."""
    command = click.option(
        "--every",
        type=click.FloatRange(min=1),
        default=None,
        metavar="SECONDS",
        help=f"Seconds between reads under --watch (default {DEFAULT_EVERY:g}, at least 1).",
    )(command)
    command = click.option(
        "--watch",
        "watch_mode",
        is_flag=True,
        help="Re-read on an interval and print only the rows that changed.",
    )(command)
    command = click.option(
        "--workspace",
        metavar="ID|NAME",
        help="One workspace, by id or exact name (default: every workspace this token can read).",
    )(command)
    return global_options(command)


def since_option(command: Callable[..., Any]) -> Callable[..., Any]:
    return click.option(
        "--since",
        default=DEFAULT_SINCE,
        show_default=True,
        metavar="SPAN|TIMESTAMP",
        help="The window: a span back from now (15m, 3h, 1d) or an ISO-8601 timestamp.",
    )(command)


# --- the verbs -----------------------------------------------------------------


@click.command()
@view_options
@click.argument("intent_id", callback=_uuid_argument)
@click.pass_context
def story(
    ctx: click.Context,
    intent_id: str,
    workspace: Optional[str],
    watch_mode: bool,
    every: Optional[float],
) -> Optional[int]:
    """The timeline of one story.

    Its state and step, every transition with who made it and through which
    channel, every provider operation with its variant and outcome, and
    everything sent for it. Exit 1 when no workspace has it (under --watch,
    waits for it to appear).

    \b
    Example:
      storydump story 66666666-6666-4666-8666-666666666666
    """
    return _run_view(
        ctx,
        "story",
        lambda client, ws: client.ops_story(ws, intent_id),
        workspace=workspace,
        watch_mode=watch_mode,
        every=every,
        missing="no such story",
    )


@click.command()
@view_options
@click.argument("intent_id", callback=_uuid_argument)
@click.pass_context
def cards(
    ctx: click.Context,
    intent_id: str,
    workspace: Optional[str],
    watch_mode: bool,
    every: Optional[float],
) -> Optional[int]:
    """Everything sent for one story, by binding and channel, in send order.

    Each row is one outbox entry with its kind, state, the channel's message
    reference, attempts and outcome — adopted twins included. Exit 1 when
    no workspace has anything for it.

    \b
    Example:
      storydump cards 66666666-6666-4666-8666-666666666666 --json
    """
    return _run_view(
        ctx,
        "cards",
        lambda client, ws: client.ops_cards(ws, intent_id),
        workspace=workspace,
        watch_mode=watch_mode,
        every=every,
        missing="no such story, or nothing sent for it yet",
    )


@click.command()
@view_options
@click.option(
    "--limit",
    type=click.IntRange(1, 500),
    default=None,
    metavar="N",
    help="At most N stories per workspace (default 100, at most 500).",
)
@click.pass_context
def floating(
    ctx: click.Context,
    limit: Optional[int],
    workspace: Optional[str],
    watch_mode: bool,
    every: Optional[float],
) -> Optional[int]:
    """Approved stories carrying a debit: step, counters, wait class and rung, next run.

    Under --watch, ends 0 once nothing is floating on two reads in a row,
    and 6 when a floating story's job has failed.

    \b
    Example:
      storydump floating --watch
    """
    return _run_view(
        ctx,
        "floating",
        lambda client, ws: client.ops_floating(ws, limit),
        workspace=workspace,
        watch_mode=watch_mode,
        every=every,
    )


@click.command()
@view_options
@click.argument("key", metavar="HANDLE|ID")
@click.pass_context
def account(
    ctx: click.Context,
    key: str,
    workspace: Optional[str],
    watch_mode: bool,
    every: Optional[float],
) -> Optional[int]:
    """One Instagram account: cap per day, today's count, the next slot, the last outcomes.

    Exit 1 when no workspace has an account by that handle or id.

    \b
    Example:
      storydump account storydump.studio
    """
    return _run_view(
        ctx,
        "account",
        lambda client, ws: client.ops_account(ws, key),
        workspace=workspace,
        watch_mode=watch_mode,
        every=every,
        missing="no such account",
    )


@click.command()
@view_options
@since_option
@click.pass_context
def jobs(
    ctx: click.Context,
    since: str,
    workspace: Optional[str],
    watch_mode: bool,
    every: Optional[float],
) -> Optional[int]:
    """The workspace's jobs by kind, lane and state: counts, the oldest due, failed samples.

    Under --watch, ends 6 when a read shows a failed group; otherwise runs
    until Ctrl-C.

    \b
    Example:
      storydump jobs --since 1h
    """
    runtime = begin(ctx, "jobs")
    window = _since(ctx, runtime, since)
    return _run_view(
        ctx,
        "jobs",
        lambda client, ws: client.ops_jobs(ws, window),
        workspace=workspace,
        watch_mode=watch_mode,
        every=every,
    )


@click.command()
@view_options
@since_option
@click.pass_context
def outbox(
    ctx: click.Context,
    since: str,
    workspace: Optional[str],
    watch_mode: bool,
    every: Optional[float],
) -> Optional[int]:
    """Pending, sending, ambiguous and failed outbox rows by binding and kind.

    Under --watch, ends 6 when a read shows a failed group; otherwise runs
    until Ctrl-C.

    \b
    Example:
      storydump outbox --watch --every 10
    """
    runtime = begin(ctx, "outbox")
    window = _since(ctx, runtime, since)
    return _run_view(
        ctx,
        "outbox",
        lambda client, ws: client.ops_outbox(ws, window),
        workspace=workspace,
        watch_mode=watch_mode,
        every=every,
    )


@click.command()
@view_options
@since_option
@click.pass_context
def burst(
    ctx: click.Context,
    since: str,
    workspace: Optional[str],
    watch_mode: bool,
    every: Optional[float],
) -> Optional[int]:
    """The post-burst read: one timeline of decisions, permits, waits, siblings, reviews and outcomes.

    Under --watch, ends 0 once no row is mid-flight (no container permitted
    and unpublished, no story in publishing), and 6 when a story was sent
    to review.

    \b
    Example:
      storydump burst --since 2026-09-15T14:50:00Z --json
    """
    runtime = begin(ctx, "burst")
    window = _since(ctx, runtime, since)
    return _run_view(
        ctx,
        "burst",
        lambda client, ws: client.ops_burst(ws, window),
        workspace=workspace,
        watch_mode=watch_mode,
        every=every,
    )


@click.command()
@global_options
@click.pass_context
def posture(ctx: click.Context) -> None:
    """The deployment's posture: the migration ledger, the connected role, RLS per table, the doors.

    Not a workspace's rows — one answer for the deployment, so there is no
    --workspace and nothing to watch.

    \b
    Example:
      storydump posture --json
    """
    runtime = begin(ctx, "posture")
    answer = signed_in_client(runtime).ops_posture()
    data = answer.get("data") if isinstance(answer, dict) else None
    if not isinstance(data, dict):
        raise Unreachable(200, None, "the answer was not the view's envelope")
    emit(envelope("posture", data), json_mode=runtime.json_mode)


COMMANDS = (story, cards, floating, account, jobs, outbox, burst, posture)
