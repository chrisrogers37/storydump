"""The write verbs: ``approve``, ``skip``, ``reject``, ``posted``, ``cancel``,
``resolve``, ``pause``, ``resume`` and ``sync`` (phase 03 of the v2 CLI).

Every one is ONE call to the command port — ``POST
/workspaces/{ws}/commands/{command}`` with the bearer token — the same door
a tap or a web click uses, so admission, tenancy and audit apply unchanged
and no verb writes to the database by any other path. The idempotency keys
are the web's (`landing/src/lib/commands.ts`), with the CLI's verdict added:
``<command>:<story>`` for a story verb, so a re-run replays ("already done",
exit 0) rather than acting twice; ``<command>:<story>:<resolution>[:<verdict>]
[:<episode>]`` for a resolution, so a later review of the same story is a new
key; and a FRESH key per invocation for ``pause``, ``resume`` and ``sync`` (a
submission id), whose effects are idempotent — a retry is harmless and a
later action always executes. ``--idempotency-key`` is the deliberate second
execution of a story verb.
``--workspace`` is required: a write goes to ONE workspace, and a name that
names two is ambiguous.

The answer is the port's outcome in the CLI's own sentence (the vocabulary
module's — never the Telegram adapter's words); a refusal is an answer, not
a failure: the reason's sentence, the fixing verb, exit 2.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional

import click

from src.services.target.vocabulary import (
    EXIT_NOT_FOUND,
    EXIT_USAGE,
    IDEMPOTENCY_KEY_MAX,
    NOT_POSTED,
    RESOLUTIONS,
    envelope,
)
from storydump_cli.client import ApiError, Client, Unreachable
from storydump_cli.commands import begin, global_options, uuid_argument
from storydump_cli.commands.auth import signed_in_client
from storydump_cli.commands.reads import WORKSPACES_FIX, workspace_targets
from storydump_cli.output import Failure, emit

SOURCES_FIX = (
    "the connected folders are under Settings › Integrations;"
    " storydump story <story> shows a story's source"
)

#: verb → the command port's command.
COMMAND_OF: Mapping[str, str] = {
    "approve": "approve",
    "skip": "skip",
    "reject": "reject",
    "posted": "mark_posted",
    "cancel": "cancel",
    "resolve": "resolve_review",
    "pause": "pause_workspace",
    "resume": "resume_workspace",
    "sync": "sync_now",
}


def deterministic_key(
    command: str,
    *,
    intent_id: str,
    resolution: Optional[str] = None,
    verdict: Optional[str] = None,
    episode: Optional[str] = None,
) -> str:
    """The web's identities (`landing/src/lib/commands.ts`), the verdict added:
    a story verb is ``<command>:<story>``, so a re-run replays; a review
    resolution is ``<command>:<story>:<resolution>[:<verdict>][:<episode>]`` —
    the episode is the story's ``entered_state_at`` (omitted when the story has
    none, as the web omits it), so a later review of the same story is a new
    key while a retry of THIS resolution replays; the verdict is the CLI's own
    addition, so a refused retry and its ``--not-posted`` retry are two keys."""
    parts = [command, intent_id]
    if resolution is not None:
        parts.append(resolution)
        if verdict is not None:
            parts.append(verdict)
        if episode:
            parts.append(str(episode))
    return ":".join(parts)


def fresh_key(command: str, *, workspace_id: str, identity: str) -> str:
    """The workspace verbs mint a fresh key per invocation — the web mints a
    submission id per click — because their effects are idempotent (pause
    and resume set a flag; a sync coalesces with a pending one): a retry is
    harmless and a later action always executes. (A day or a minute bucket
    answered "already done" to a second pause after a resume.)"""
    return f"{command}:{workspace_id}:{identity}"


def _one_workspace(client: Client, workspace: str) -> str:
    """The one workspace a write goes to: an id as given, a name through the
    principal — and a name two workspaces share needs the id."""
    ids = workspace_targets(client, workspace)
    if len(ids) > 1:
        raise Failure(
            code=EXIT_USAGE,
            reason="usage",
            detail=f"{workspace!r} names {len(ids)} workspaces — pass the workspace id",
            fix=WORKSPACES_FIX,
        )
    return ids[0]


def _check_key(idempotency_key: Optional[str]) -> Optional[str]:
    if idempotency_key is None:
        return None
    key = idempotency_key.strip()
    if not key or len(key) > IDEMPOTENCY_KEY_MAX:
        raise click.BadParameter(
            f"an idempotency key is 1 to {IDEMPOTENCY_KEY_MAX} characters",
            param_hint="--idempotency-key",
        )
    return key


KeyFor = Callable[[Any, Client, str], str]


def _story_key(command: str, intent_id: str) -> KeyFor:
    return lambda runtime, client, ws: deterministic_key(command, intent_id=intent_id)


def _fresh_key(verb: str) -> KeyFor:
    return lambda runtime, client, ws: fresh_key(
        COMMAND_OF[verb], workspace_id=ws, identity=runtime.uuid_fn()
    )


def _episode_of(client: Client, ws: str, intent_id: str) -> str:
    """The review episode: the story's `entered_state_at`, read through the
    story view; no such story is the answer before anything is sent."""
    document = client.ops_story(ws, intent_id)
    data = document.get("data") if isinstance(document, dict) else None
    rows = data.get("rows") if isinstance(data, dict) else None
    row = (
        rows[0]
        if isinstance(rows, list) and rows and isinstance(rows[0], dict)
        else None
    )
    if row is None:
        raise Failure(
            code=EXIT_NOT_FOUND,
            reason="not_found",
            detail="no such story in this workspace",
            fix="storydump floating and storydump story <story> find a story",
        )
    intent = row.get("intent") if isinstance(row.get("intent"), dict) else {}
    return str(intent.get("entered_state_at") or "")


def _write(
    ctx: click.Context,
    verb: str,
    *,
    workspace: str,
    idempotency_key: Optional[str],
    args: dict[str, Any],
    key_for: KeyFor,
) -> None:
    runtime = begin(ctx, verb)
    command = COMMAND_OF[verb]
    key = _check_key(idempotency_key)
    client = signed_in_client(runtime)
    ws = _one_workspace(client, workspace)
    if key is None:
        key = key_for(runtime, client, ws)
    try:
        answer = client.command(ws, command, args, idempotency_key=key)
    except ApiError as exc:
        if verb == "sync" and exc.reason == "not_found":
            # the port's `not_found` names a media source here; the shared
            # sentence would say "story"
            raise Failure(
                code=EXIT_NOT_FOUND,
                reason="not_found",
                detail="no such media source in this workspace",
                fix=SOURCES_FIX,
            ) from None
        raise
    outcome = answer.get("outcome") if isinstance(answer, dict) else None
    if not isinstance(outcome, str) or not outcome:
        raise Unreachable(200, None, "the answer was not the command port's")
    result = {k: v for k, v in answer.items() if k != "outcome"}
    data = {
        "workspace_id": ws,
        "command": command,
        "args": dict(args),
        "idempotency_key": key,
        "outcome": outcome,
        "result": result,
    }
    emit(envelope(verb, data), json_mode=runtime.json_mode)


def _workspace_option(command):
    return click.option(
        "--workspace",
        required=True,
        metavar="ID|NAME",
        help="The workspace to act in: its id, or its exact name (one match only).",
    )(command)


def _key_option(command):
    return click.option(
        "--idempotency-key",
        "idempotency_key",
        default=None,
        metavar="KEY",
        help=(
            "Send under a key of your own. A story verb's key is deterministic"
            " (a re-run replays); pause, resume and sync mint a fresh one."
        ),
    )(command)


def _story_verb(name: str, doc: str):
    """A verb on one story: ``storydump <verb> <story> --workspace <ws>``."""

    @click.command(name=name, help=doc)
    @global_options
    @_workspace_option
    @_key_option
    @click.argument("story", callback=uuid_argument)
    @click.pass_context
    def verb(
        ctx: click.Context, workspace: str, idempotency_key: Optional[str], story: str
    ):
        _write(
            ctx,
            name,
            workspace=workspace,
            idempotency_key=idempotency_key,
            args={"intent_id": story},
            key_for=_story_key(COMMAND_OF[name], story),
        )

    return verb


approve = _story_verb(
    "approve",
    """Approve a story: it posts through the Instagram API at its slot.

    Refused with `manual_mode` where Instagram API posting is off for the
    workspace, and `not_connected` where no Instagram account is connected.

    \b
    Example:
      storydump approve 4ddec0e0-1c2b-4f3a-8e9d-5b6a7c8d9e0f --workspace "Chris's studio"
    """,
)
skip = _story_verb(
    "skip",
    """Skip a story awaiting approval; the slot passes.

    \b
    Example:
      storydump skip 4ddec0e0-1c2b-4f3a-8e9d-5b6a7c8d9e0f --workspace <id>
    """,
)
reject = _story_verb(
    "reject",
    """Reject a story awaiting approval.

    \b
    Example:
      storydump reject 4ddec0e0-1c2b-4f3a-8e9d-5b6a7c8d9e0f --workspace <id>
    """,
)
posted = _story_verb(
    "posted",
    """Record that you posted a story by hand (the manual path).

    \b
    Example:
      storydump posted 4ddec0e0-1c2b-4f3a-8e9d-5b6a7c8d9e0f --workspace <id>
    """,
)
cancel = _story_verb(
    "cancel",
    """Cancel a story: a waiting story is refunded and its upload destroyed;
    a story mid-flight stops at its next step.

    \b
    Example:
      storydump cancel 4ddec0e0-1c2b-4f3a-8e9d-5b6a7c8d9e0f --workspace <id>
    """,
)


@click.command()
@global_options
@_workspace_option
@_key_option
@click.argument("story", callback=uuid_argument)
@click.argument("resolution", type=click.Choice(RESOLUTIONS))
@click.option(
    "--not-posted",
    "not_posted",
    is_flag=True,
    help=(
        "Your verdict for a retry after a lost publish answer: you looked, and"
        " the story is not on Instagram."
    ),
)
@click.pass_context
def resolve(
    ctx: click.Context,
    workspace: str,
    idempotency_key: Optional[str],
    story: str,
    resolution: str,
    not_posted: bool,
) -> None:
    """Resolve a story parked for review: retry it, record that it posted,
    or give up (its debit is retained).

    A retry after a LOST publish answer is refused (`may_have_posted`)
    unless --not-posted carries your verdict. The key carries the story's
    review episode, read from the story view first (a --idempotency-key of
    your own skips that read).

    \b
    Examples:
      storydump resolve 4ddec0e0-1c2b-4f3a-8e9d-5b6a7c8d9e0f retry --workspace <id>
      storydump resolve 4ddec0e0-1c2b-4f3a-8e9d-5b6a7c8d9e0f retry --not-posted --workspace <id>
      storydump resolve 4ddec0e0-1c2b-4f3a-8e9d-5b6a7c8d9e0f posted --workspace <id>
    """
    args: dict[str, Any] = {"intent_id": story, "resolution": resolution}
    if not_posted:
        args["verdict"] = NOT_POSTED

    def key_for(runtime: Any, client: Client, ws: str) -> str:
        return deterministic_key(
            COMMAND_OF["resolve"],
            intent_id=story,
            resolution=resolution,
            verdict=NOT_POSTED if not_posted else None,
            episode=_episode_of(client, ws, story),
        )

    _write(
        ctx,
        "resolve",
        workspace=workspace,
        idempotency_key=idempotency_key,
        args=args,
        key_for=key_for,
    )


def _workspace_verb(name: str, doc: str):
    """A verb on the workspace itself: ``storydump <verb> --workspace <ws>``."""

    @click.command(name=name, help=doc)
    @global_options
    @_workspace_option
    @_key_option
    @click.pass_context
    def verb(ctx: click.Context, workspace: str, idempotency_key: Optional[str]):
        _write(
            ctx,
            name,
            workspace=workspace,
            idempotency_key=idempotency_key,
            args={},
            key_for=_fresh_key(name),
        )

    return verb


pause = _workspace_verb(
    "pause",
    """Pause posting for the workspace: nothing posts until `resume`.

    \b
    Example:
      storydump pause --workspace "Chris's studio"
    """,
)
resume = _workspace_verb(
    "resume",
    """Resume posting for a paused workspace.

    \b
    Example:
      storydump resume --workspace "Chris's studio"
    """,
)


@click.command()
@global_options
@_workspace_option
@_key_option
@click.argument("source", callback=uuid_argument)
@click.pass_context
def sync(
    ctx: click.Context, workspace: str, idempotency_key: Optional[str], source: str
) -> None:
    """Queue a sync of one connected media source (its id from the web's
    Integrations tab, or `storydump story <id>`'s source).

    \b
    Example:
      storydump sync 5a5a5a5a-5a5a-4a5a-8a5a-5a5a5a5a5a5a --workspace <id>
    """
    _write(
        ctx,
        "sync",
        workspace=workspace,
        idempotency_key=idempotency_key,
        args={"source_id": source},
        key_for=_fresh_key("sync"),
    )


COMMANDS = (approve, skip, reject, posted, cancel, resolve, pause, resume, sync)
