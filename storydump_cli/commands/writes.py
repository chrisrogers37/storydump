"""The write verbs: ``approve``, ``skip``, ``reject``, ``posted``, ``cancel``,
``resolve``, ``pause``, ``resume`` and ``sync``, and
``schedule`` and ``reschedule``, which plan a story (#1413) and may
make it repeat (#485).

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
later action always executes. ``schedule`` and ``reschedule`` take a fresh key
too: planning an item again after a cancel, or moving a story back to a time it
had, is a new act, and a duplicate schedule is the database's to refuse (the
item already waiting on that account). ``--idempotency-key`` is the deliberate
second execution of a story verb.
``--workspace`` is required: a write goes to ONE workspace, and a name that
names two is ambiguous.

The answer is the port's outcome in the CLI's own sentence (the vocabulary
module's — never the Telegram adapter's words); a refusal is an answer, not
a failure: the reason's sentence, the fixing verb, exit 2.

``admit`` (#1723) is the one write outside the port: admitting a person to
sign up belongs to no workspace, and the command route is keyed on one. It
is one call to ``POST /admissions``, with the address in the body, and takes
no ``--workspace``. A re-run answers "already admitted", exit 0, as a replay
does; its refusals take the same path as the port's.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional

import click

from src.services.target.vocabulary import (
    ADMIT_SENTENCES,
    AT_RULE_SENTENCES,
    EXIT_NOT_FOUND,
    EXIT_USAGE,
    IDEMPOTENCY_KEY_MAX,
    IN_THE_WAY,
    LINK_URL_MAX,
    LIVE_ACCOUNT_STATES,
    MISSING_SENTENCES,
    NOT_POSTED,
    PLAN_HORIZON_DAYS,
    REPEAT_SENTENCES,
    REPEAT_UNITS,
    RESOLUTIONS,
    WEEKDAYS,
    envelope,
    exit_code_for,
)
from storydump_cli.client import ApiError, Client, Unreachable
from storydump_cli.commands import as_uuid, begin, global_options, uuid_argument
from storydump_cli.commands.auth import signed_in_client
from storydump_cli.commands.reads import WORKSPACES_FIX, unwrap, workspace_targets
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
    "schedule": "schedule_item",
    "reschedule": "reschedule_item",
    "link": "set_item_link",
}


def deterministic_key(
    command: str,
    *,
    intent_id: str,
    resolution: Optional[str] = None,
    verdict: Optional[str] = None,
    episode: Optional[str] = None,
    stop_repeat: bool = False,
) -> str:
    """The web's identities (`landing/src/lib/commands.ts`), the verdict added:
    a story verb is ``<command>:<story>``, so a re-run replays; a review
    resolution is ``<command>:<story>:<resolution>[:<verdict>][:<episode>]`` —
    the episode is the story's ``entered_state_at`` (omitted when the story has
    none, as the web omits it), so a later review of the same story is a new
    key while a retry of THIS resolution replays; the verdict is the CLI's own
    addition, so a refused retry and its ``--not-posted`` retry are two keys.
    ``cancel --stop-repeat`` is ``cancel:<story>:stop_repeat`` for the same
    reason: a plain cancel and one that also ends the series are two requests
    (#485)."""
    parts = [command, intent_id]
    if stop_repeat:
        parts.append("stop_repeat")
    if resolution is not None:
        parts.append(resolution)
        if verdict is not None:
            parts.append(verdict)
        if episode:
            parts.append(str(episode))
    return ":".join(parts)


def fresh_key(command: str, *, workspace_id: str, identity: str) -> str:
    """The workspace verbs, and the two that plan a story, are keyed per
    ATTEMPT, as the web's submission id is: a fresh identity per invocation,
    because an invocation IS an attempt and a re-run is a new one by design —
    pause and resume set a flag and a sync coalesces with a pending one, so a
    retry is harmless and a later action always executes; a second schedule
    of the same item is the database's to refuse, and planning it again after
    a cancel, or moving a story back to a time it had, is a new act. (A day
    or a minute bucket would answer "already done" to a second pause after a
    resume.) The next entity-less verb follows the same rule; the identities
    both doors derive are pinned in `tests/fixtures/idempotency_keys.json`."""
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
    if not key.isascii() or not key.isprintable():
        # it travels as a header: ASCII by construction, and a newline in one
        # is a request the transport refuses with a traceback
        raise click.BadParameter(
            "an idempotency key is printable ASCII on one line",
            param_hint="--idempotency-key",
        )
    return key


KeyFor = Callable[[Any, Client, str], str]


def _story_key(command: str, intent_id: str, **kw: Any) -> KeyFor:
    return lambda runtime, client, ws: deterministic_key(
        command, intent_id=intent_id, **kw
    )


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


#: A verb's own words for a refusal whose shared sentence would be wrong for
#: it — ``(detail, fix)`` — or None, which leaves it to the shared sentence.
#: The exit code stays the vocabulary's (`exit_code_for`), whichever words.
Refused = Callable[[ApiError], Optional[tuple[str, str]]]
#: Arguments only the workspace can resolve (a handle to an id).
Resolve = Callable[[Client, str], dict[str, Any]]


def _write(
    ctx: click.Context,
    verb: str,
    *,
    workspace: str,
    idempotency_key: Optional[str],
    args: dict[str, Any],
    key_for: KeyFor,
    resolve: Optional[Resolve] = None,
    refused: Optional[Refused] = None,
) -> None:
    runtime = begin(ctx, verb)
    command = COMMAND_OF[verb]
    key = _check_key(idempotency_key)
    client = signed_in_client(runtime)
    ws = _one_workspace(client, workspace)
    if resolve is not None:
        args = {**args, **resolve(client, ws)}
    if key is None:
        key = key_for(runtime, client, ws)
    try:
        answer = client.command(ws, command, args, idempotency_key=key)
    except ApiError as exc:
        words = refused(exc) if refused is not None else None
        if words is not None:
            detail, fix = words
            raise Failure(
                code=exit_code_for(exc.status, exc.reason),
                reason=exc.reason or "refused",
                detail=detail,
                fix=fix,
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
            " (a re-run replays); schedule, reschedule, link, pause, resume and"
            " sync mint a fresh one per run."
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
      storydump approve 2b2b2b2b-2b2b-4b2b-8b2b-2b2b2b2b2b2b --workspace "Example studio"
    """,
)
skip = _story_verb(
    "skip",
    """Skip a story awaiting approval; the slot passes.

    \b
    Example:
      storydump skip 2b2b2b2b-2b2b-4b2b-8b2b-2b2b2b2b2b2b --workspace <id>
    """,
)
reject = _story_verb(
    "reject",
    """Reject a story awaiting approval.

    \b
    Example:
      storydump reject 2b2b2b2b-2b2b-4b2b-8b2b-2b2b2b2b2b2b --workspace <id>
    """,
)
posted = _story_verb(
    "posted",
    """Record that you posted a story by hand (the manual path).

    \b
    Example:
      storydump posted 2b2b2b2b-2b2b-4b2b-8b2b-2b2b2b2b2b2b --workspace <id>
    """,
)


@click.command()
@global_options
@_workspace_option
@_key_option
@click.argument("story", callback=uuid_argument)
@click.option(
    "--stop-repeat",
    "stop_repeat",
    is_flag=True,
    help="End a repeating story's series as well: no next occurrence is made.",
)
@click.pass_context
def cancel(
    ctx: click.Context,
    workspace: str,
    idempotency_key: Optional[str],
    story: str,
    stop_repeat: bool,
) -> None:
    """Cancel a story: a waiting story is refunded and its upload destroyed;
    a story mid-flight stops at its next step. A repeating story's next
    occurrence is still made, unless --stop-repeat ends the series too. On a
    story already being cancelled, --stop-repeat ends its series.

    \b
    Examples:
      storydump cancel 2b2b2b2b-2b2b-4b2b-8b2b-2b2b2b2b2b2b --workspace <id>
      storydump cancel 2b2b2b2b-2b2b-4b2b-8b2b-2b2b2b2b2b2b --stop-repeat --workspace <id>
    """
    args: dict[str, Any] = {"intent_id": story}
    if stop_repeat:
        args["stop_repeat"] = True
    _write(
        ctx,
        "cancel",
        workspace=workspace,
        idempotency_key=idempotency_key,
        args=args,
        key_for=_story_key(COMMAND_OF["cancel"], story, stop_repeat=stop_repeat),
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
      storydump resolve 2b2b2b2b-2b2b-4b2b-8b2b-2b2b2b2b2b2b retry --workspace <id>
      storydump resolve 2b2b2b2b-2b2b-4b2b-8b2b-2b2b2b2b2b2b retry --not-posted --workspace <id>
      storydump resolve 2b2b2b2b-2b2b-4b2b-8b2b-2b2b2b2b2b2b posted --workspace <id>
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
      storydump pause --workspace "Example studio"
    """,
)
resume = _workspace_verb(
    "resume",
    """Resume posting for a paused workspace.

    \b
    Example:
      storydump resume --workspace "Example studio"
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
        refused=_sync_refused,
    )


def _sync_refused(exc: ApiError) -> Optional[tuple[str, str]]:
    # the port's `not_found` names a media source here; the shared sentence
    # would say "story"
    if exc.reason == "not_found":
        return "no such media source in this workspace", SOURCES_FIX
    return None


# --- planning a story (#1413) ---------------------------------------------------

AT_HELP = (
    "When, as YYYY-MM-DD HH:MM in the account's own time zone (the workspace's"
    " when the account has none), with no offset."
)
AT_FIX = (
    "give --at as 'YYYY-MM-DD HH:MM' in the account's zone, after now and"
    f" within {PLAN_HORIZON_DAYS} days"
)
#: Where to look for what the port could not find, by its `missing` fact
#: (the keys of `MISSING_SENTENCES`).
MISSING_FIXES = {
    "account": "storydump account <handle> shows an account",
    "item": "check the item's id: storydump story <story> shows a story's item as media",
}


def _at_option(*, required: bool = True):
    return click.option(
        "--at", "local_at", required=required, metavar="WHEN", help=AT_HELP
    )


def _time_refused(exc: ApiError) -> Optional[tuple[str, str]]:
    """A time the port refused, by the rule it broke (its `at_rule`)."""
    rule = exc.facts.get("at_rule")
    if exc.reason == "invalid_args" and rule in AT_RULE_SENTENCES:
        return AT_RULE_SENTENCES[rule], AT_FIX
    return None


#: What a weekly or daily rule takes, for a rule the port refused.
REPEAT_FIX = (
    "--every counts days for --repeat day and weeks for --repeat week;"
    " --on names a weekly repeat's days, e.g. mon,thu"
)

#: The fix for each `repeat` fact the port refuses with.
REPEAT_FIXES = {
    "invalid": REPEAT_FIX,
    "off_day": "add the story's own day to --on, or move it to one of the series' days",
}


def _repeat_refused(exc: ApiError) -> Optional[tuple[str, str]]:
    """A repeat rule the port refused, by its `repeat` fact: one the ledger's
    CHECKs refused, or a weekly series whose days leave out its story's."""
    fact = exc.facts.get("repeat")
    if exc.reason == "invalid_args" and fact in REPEAT_SENTENCES:
        return REPEAT_SENTENCES[fact], REPEAT_FIXES[fact]
    return None


def _weekdays(
    ctx: click.Context, param: click.Parameter, value: Optional[str]
) -> Optional[list[int]]:
    """``--on mon,thu`` as ISO weekdays: a name's place in `WEEKDAYS`, plus one."""
    if value is None:
        return None
    names = [name.strip().lower() for name in value.split(",") if name.strip()]
    if not names or any(name not in WEEKDAYS for name in names):
        raise click.BadParameter(
            f"weekdays are {','.join(WEEKDAYS)}, comma-separated", ctx=ctx, param=param
        )
    return [WEEKDAYS.index(name) + 1 for name in names]


def _repeat_options(command):
    """``--repeat``, ``--every`` and ``--on``: a planned story's repeat rule."""
    for option in (
        click.option(
            "--on",
            "on",
            default=None,
            metavar="DAYS",
            callback=_weekdays,
            help=(
                "A weekly repeat's days, e.g. mon,thu, the day it is due among them"
                " (by default a weekly series keeps its days, and a new one takes"
                " the day it is due)."
            ),
        ),
        click.option(
            "--every",
            "every",
            type=int,
            default=None,
            metavar="N",
            help="Every N days or weeks (by default 1).",
        ),
        click.option(
            "--repeat",
            "repeat",
            type=click.Choice(REPEAT_UNITS),
            default=None,
            help=(
                "Repeat it every day or every week. The time chosen is the first"
                " occurrence, and each one is asked about as any story is."
            ),
        ),
    ):
        command = option(command)
    return command


def _repeat_body(
    repeat: Optional[str], every: Optional[int], on: Optional[list[int]]
) -> Optional[dict[str, Any]]:
    """The ``repeat`` a write sends, or None for none. ``--every`` and ``--on``
    shape a rule, so they need ``--repeat``; ``--on`` is a weekly rule's."""
    if repeat is None:
        if every is not None or on is not None:
            raise click.UsageError("--every and --on shape a repeat: add --repeat")
        return None
    if on is not None and repeat != "week":
        raise click.UsageError("--on names a weekly repeat's days: use --repeat week")
    rule: dict[str, Any] = {"unit": repeat}
    if every is not None:
        rule["every"] = every
    if on is not None:
        rule["days"] = on
    return rule


def _schedule_refused(exc: ApiError) -> Optional[tuple[str, str]]:
    if exc.reason == "locked":
        in_the_way = "; ".join(
            IN_THE_WAY.get(str(kind), str(kind))
            for kind in exc.facts.get("in_the_way") or []
        )
        if exc.facts.get("overridable") is True:
            return (
                f"this item is held back: {in_the_way}",
                "run it again with --override-locks to schedule it anyway",
            )
        return (
            f"this item cannot be scheduled: {in_the_way}",
            "pick another item — --override-locks does not get past this",
        )
    if exc.reason == "not_found" and exc.facts.get("missing") in MISSING_SENTENCES:
        missing = exc.facts["missing"]
        return MISSING_SENTENCES[missing], MISSING_FIXES[missing]
    if exc.reason == "illegal_transition":
        existing = exc.facts.get("existing")
        if isinstance(existing, dict) and existing.get("intent_id"):
            story = existing["intent_id"]
            if existing.get("cancel_requested"):
                return (
                    f"that item's story on that account, {story}, is still being"
                    " cancelled",
                    f"run it again once the cancel has landed (storydump story"
                    f" {story} shows it)",
                )
            if existing.get("origin") == "planned":
                # a re-run after an answer that never arrived meets its own story
                whose = "if you just ran this, it is the story you planned; otherwise "
            else:
                whose = "the cadence picked that item for that account; "
            return (
                f"that item already waits on that account: story {story}"
                f" ({existing.get('origin')}, {existing.get('state')})",
                f"storydump story {story} shows it: {whose}cancel it to plan the"
                " item again",
            )
        return (
            "that item is already waiting to post on that account",
            "run it again, since the story in the way may have just ended; if it is"
            " refused again, storydump account <handle> shows that account's stories",
        )
    return _repeat_refused(exc) or _time_refused(exc)


def _reschedule_refused(exc: ApiError) -> Optional[tuple[str, str]]:
    if exc.reason == "illegal_transition":
        return (
            "only a planned story still waiting for its time can move or take a"
            " repeat, and only one not yet ended can stop repeating",
            "storydump story <story> shows where the story is",
        )
    return _repeat_refused(exc) or _time_refused(exc)


def _account_id(client: Client, ws: str, key: str) -> str:
    """The account by id as given, or by handle through the account view —
    the one LIVE account the handle names. A handle is not unique (a removed
    or moved destination keeps it), so two live matches need the id."""
    account_id = as_uuid(key)
    if account_id is not None:
        return account_id
    rows = unwrap(client.ops_account(ws, key), ws)["rows"]
    # A row with no `state` comes from an API older than this CLI: sent as it
    # is, the API's own refusal is the answer, never a false "no live account".
    live = [
        row
        for row in rows
        if isinstance(row, dict)
        and ("state" not in row or row["state"] in LIVE_ACCOUNT_STATES)
    ]
    if not live:
        raise Failure(
            code=EXIT_NOT_FOUND,
            reason="not_found",
            detail=f"no live account {key!r} in this workspace",
            fix="storydump account <handle> finds one; its id works too",
        )
    if len(live) > 1:
        raise Failure(
            code=EXIT_USAGE,
            reason="usage",
            detail=f"{key!r} names {len(live)} live accounts — pass the account id",
            fix="storydump account <handle> lists them with their ids",
        )
    return str(live[0]["id"])


@click.command()
@global_options
@_workspace_option
@_key_option
@click.argument("item", callback=uuid_argument)
@click.option(
    "--account",
    required=True,
    metavar="HANDLE|ID",
    help="The Instagram account to post it on: its handle (the @ optional) or id.",
)
@_at_option()
@click.option(
    "--override-locks",
    "override_locks",
    is_flag=True,
    help=(
        "Schedule it although it was skipped or posted recently. A rejection, a"
        " hold, an item out of season or one that cannot post still refuses."
    ),
)
@_repeat_options
@click.pass_context
def schedule(
    ctx: click.Context,
    workspace: str,
    idempotency_key: Optional[str],
    item: str,
    account: str,
    local_at: str,
    override_locks: bool,
    repeat: Optional[str],
    every: Optional[int],
    on: Optional[list[int]],
) -> None:
    """Plan a story: this item, on this account, at this time. At its time the
    workspace's chats are asked to approve it, as for any story — nothing
    posts by itself — and a story that cannot be served then is ended and
    said, never dropped.

    With --repeat it repeats: --at is the first occurrence, and when one
    ends the next is planned for the rule's next time.

    Refused with `locked` when the item cannot be posted or a lock is in the
    way (a skip or a recent post gives way to --override-locks), and with
    `illegal_transition` when the item already waits on that account.

    \b
    Examples:
      storydump schedule 3c3c3c3c-3c3c-4c3c-8c3c-3c3c3c3c3c3c --account example.brand --at "2026-10-12 18:30" --workspace <id>
      storydump schedule 3c3c3c3c-3c3c-4c3c-8c3c-3c3c3c3c3c3c --account example.brand --at "2026-10-15 16:07" --repeat week --on mon,thu --workspace <id>
    """
    if not account.strip():
        raise click.BadParameter(
            "an account is a handle or an id", param_hint="--account"
        )
    rule = _repeat_body(repeat, every, on)
    args: dict[str, Any] = {"media_item_id": item, "local_at": local_at}
    if override_locks:
        args["override_locks"] = True
    if rule is not None:
        args["repeat"] = rule
    _write(
        ctx,
        "schedule",
        workspace=workspace,
        idempotency_key=idempotency_key,
        args=args,
        key_for=_fresh_key("schedule"),
        resolve=lambda client, ws: {"ig_account_id": _account_id(client, ws, account)},
        refused=_schedule_refused,
    )


@click.command()
@global_options
@_workspace_option
@_key_option
@click.argument("story", callback=uuid_argument)
@_at_option(required=False)
@_repeat_options
@click.option(
    "--no-repeat",
    "no_repeat",
    is_flag=True,
    help="Stop it repeating: it keeps its time, and no next occurrence is made.",
)
@click.pass_context
def reschedule(
    ctx: click.Context,
    workspace: str,
    idempotency_key: Optional[str],
    story: str,
    local_at: Optional[str],
    repeat: Optional[str],
    every: Optional[int],
    on: Optional[list[int]],
    no_repeat: bool,
) -> None:
    """Move a planned story to another time, or set or stop its repeat, while
    it still waits for its time; --no-repeat also stops a story already
    served, or one being cancelled. Moving a repeating story moves its series
    to the new time of day. A weekly series moves to one of its days, or takes
    the new day when it has only one. Its item and account stay: to change
    those, cancel it and schedule again.

    \b
    Examples:
      storydump reschedule 2b2b2b2b-2b2b-4b2b-8b2b-2b2b2b2b2b2b --at "2026-10-13 09:00" --workspace <id>
      storydump reschedule 2b2b2b2b-2b2b-4b2b-8b2b-2b2b2b2b2b2b --repeat day --every 2 --workspace <id>
      storydump reschedule 2b2b2b2b-2b2b-4b2b-8b2b-2b2b2b2b2b2b --no-repeat --workspace <id>
    """
    if no_repeat and (repeat or every is not None or on is not None):
        raise click.UsageError(
            "--no-repeat stops a repeat: drop --repeat, --every and --on"
        )
    rule = _repeat_body(repeat, every, on)
    if local_at is None and rule is None and not no_repeat:
        raise click.UsageError("give --at, --repeat or --no-repeat")
    args: dict[str, Any] = {"intent_id": story}
    if local_at is not None:
        args["local_at"] = local_at
    if rule is not None or no_repeat:
        args["repeat"] = rule
    _write(
        ctx,
        "reschedule",
        workspace=workspace,
        idempotency_key=idempotency_key,
        args=args,
        key_for=_fresh_key("reschedule"),
        refused=_reschedule_refused,
    )


def _link_refused(exc: ApiError) -> Optional[tuple[str, str]]:
    """The item id is checked before anything is sent, so the port refuses
    only the link; the shared sentence would name none of its rule."""
    if exc.reason == "invalid_args":
        return (
            f"a link is an https:// address of at most {LINK_URL_MAX:,} characters,"
            " with no spaces and no user name or password in it",
            "give the whole link, starting https://, or --clear to remove it",
        )
    if exc.reason == "not_found" and exc.facts.get("missing") in MISSING_SENTENCES:
        missing = exc.facts["missing"]
        return MISSING_SENTENCES[missing], MISSING_FIXES[missing]
    return None


@click.command()
@global_options
@_workspace_option
@_key_option
@click.argument("item", callback=uuid_argument)
@click.argument("url", required=False)
@click.option("--clear", is_flag=True, help="Remove the item's link instead.")
@click.pass_context
def link(
    ctx: click.Context,
    workspace: str,
    idempotency_key: Optional[str],
    item: str,
    url: Optional[str],
    clear: bool,
) -> None:
    """Give an item the link its stories ask a person to add by hand when they
    post, or remove it with --clear. A story published through the API cannot
    carry a link sticker, so its approval card shows the link instead. The
    link is the item's: every story of it asks for the same one.

    Only an https:// address is taken, and an overlong one is refused.

    \b
    Example:
      storydump link 3c3c3c3c-3c3c-4c3c-8c3c-3c3c3c3c3c3c https://example.com/spring --workspace <id>
      storydump link 3c3c3c3c-3c3c-4c3c-8c3c-3c3c3c3c3c3c --clear --workspace <id>
    """
    if clear == (url is not None):
        raise click.UsageError("give a link, or --clear to remove it: one of the two")
    _write(
        ctx,
        "link",
        workspace=workspace,
        idempotency_key=idempotency_key,
        args={"media_item_id": item, "link_url": None if clear else url},
        key_for=_fresh_key("link"),
        refused=_link_refused,
    )


# --- admitting a person (#1723) ---------------------------------------------------

#: What an interrupted `admit` says instead of the shared `INTERRUPTED_FIX`
#: (#1740): an admission carries no idempotency key, so a re-run is not safe
#: by a key. Before the request left, nothing was sent.
ADMIT_NOT_SENT_FIX = "no admission request was sent; run this command again when ready"
#: After it may have left, the answer is uncertain. A plain admission is safe
#: to repeat on its own terms: an address admitted before is answered already
#: admitted, and nothing is emailed again.
ADMIT_UNCERTAIN_FIX = (
    "the answer is uncertain; run ordinary admit again without --resend when"
    " ready. If the address was already admitted, it sends no additional email"
)
#: A resend is not: a second one may queue another email whose link replaces
#: the first one's.
RESEND_UNCERTAIN_FIX = (
    "the answer is uncertain; the email may already be queued. Check storydump"
    " waitlist before deciding to send again; repeating --resend may queue"
    " another email and replace the previous claim link"
)


def _interrupted(fix: str) -> Failure:
    """The dispatcher's interruption answer — exit 64, its reason and detail —
    with the admission's own fix."""
    return Failure(
        code=EXIT_USAGE,
        reason="interrupted",
        detail="interrupted before the verb answered",
        fix=fix,
    )


@click.command()
@global_options
@click.argument("email")
@click.option(
    "--resend",
    is_flag=True,
    help=(
        "Email an address admitted before again; its new link replaces the last one."
    ),
)
@click.pass_context
def admit(ctx: click.Context, email: str, resend: bool) -> None:
    """Admit a person to sign up, and email them a one-time sign-up link.

    Any address, on the waitlist or not. Through the link they sign up once,
    with any Google account; signing in as the address itself works too. A
    re-run answers already admitted and sends nothing, and --resend sends
    the email again. For the people in the API's OPS_USER_IDS. An admission
    belongs to no workspace, so there is no --workspace. It emails a real
    person and nothing undoes it: the owner's decision, never an agent's.

    \b
    Examples:
      storydump admit person@example.com
      storydump admit person@example.com --resend
    """
    runtime = begin(ctx, "admit")
    try:
        client = signed_in_client(runtime)
    except (KeyboardInterrupt, EOFError, click.Abort):
        raise _interrupted(ADMIT_NOT_SENT_FIX) from None
    try:
        answer = client.admit(email, resend)
    except (KeyboardInterrupt, EOFError, click.Abort):
        # Entering the request proves nothing about whether it reached the
        # API, so the answer says "may", never "was" or "was not".
        raise _interrupted(
            RESEND_UNCERTAIN_FIX if resend else ADMIT_UNCERTAIN_FIX
        ) from None
    outcome = answer.get("outcome") if isinstance(answer, dict) else None
    if outcome not in ADMIT_SENTENCES:
        raise Unreachable(200, None, "the answer was not the admission route's")
    emit(
        envelope("admit", {"email": email, "resend": resend, "outcome": outcome}),
        json_mode=runtime.json_mode,
    )


COMMANDS = (
    approve,
    skip,
    reject,
    posted,
    cancel,
    resolve,
    pause,
    resume,
    sync,
    schedule,
    reschedule,
    link,
    admit,
)
