"""The verbs, and the two conventions they share.

Every verb accepts ``--json`` and ``--api`` in any position — after the
verb is where a hand types them — so the options are declared once here
and applied to the root group and to each verb; their callbacks write onto
the ``Runtime`` on the context and expose nothing to the verb's signature.
Every verb calls ``begin`` first: it names the verb for the envelope
(``main`` needs the kind when it renders a failure the verb never saw) and
hands back the runtime.
"""

from __future__ import annotations

import uuid
from typing import Any, Callable, Mapping, Optional

import click

from storydump_cli.config import API_URL_ENV, DEFAULT_API_URL

UNREACHABLE_FIX = "check STORYDUMP_API and the network"

#: What fixes each authorization answer, by the vocabulary's reason.
FIXES: Mapping[str, str] = {
    "not_authorized": "run storydump login with a token minted on the web under Settings › API tokens",
    "session_required": "do this signed in on the web; a token cannot",
    "readonly_token": "use a token minted with the operator role",
    "wrong_workspace": "use a token minted for that workspace, or a person-bound token",
    "not_a_member": "check the workspace id, and this token's workspaces with storydump whoami",
    # the command port's refusals (phase 03): the fixing verb, named
    "manual_mode": "turn Instagram API posting on under Settings › General, or approve on the web",
    "not_connected": "connect the Instagram account under Settings › Accounts",
    "may_have_posted": (
        "look at Instagram, then storydump resolve <story> retry --not-posted"
        " or storydump resolve <story> posted"
    ),
    "illegal_transition": "storydump story <story> shows the state the story is in",
    "not_found": "storydump floating and storydump story <story> find a story",
    "nothing_to_confirm": "storydump story <story> shows what Instagram was asked",
    "cancelling": "wait for the cancel to land; storydump story <story> shows it",
    "invalid_args": "see storydump <verb> --help",
    "workspace_required": "pass --workspace <id or name>",
    "admission_conflict": "pass --idempotency-key <a new key> to send a different command",
    # the audit fold (2026-09-16): a bare 403 is the role floor; a 503 naming
    # `pool_saturated` is the API shedding load
    "insufficient_role": "ask a workspace admin or owner to raise your role, or do it as one",
    "pool_saturated": "wait a moment and run it again — a write's key makes a re-run safe",
}
INTERRUPTED_FIX = (
    "run it again — a read has no effect, and a write's idempotency key makes"
    " a re-run safe"
)
DEFAULT_FIX = "see storydump --help"
#: What fixes a config file the CLI cannot use, when the error names no fix.
DEFAULT_CONFIG_FIX = "fix the config file, or delete it and run storydump login again"


def runtime_of(ctx: click.Context) -> Any:
    """The ``Runtime`` on the context (``main`` always puts one there)."""
    runtime = ctx.obj
    if runtime is None:
        raise RuntimeError(
            "no Runtime on the context — invoke through storydump_cli.main"
        )
    return runtime


def begin(ctx: click.Context, kind: str) -> Any:
    """Name the verb for its envelope and hand back the runtime."""
    runtime = runtime_of(ctx)
    runtime.kind = kind
    return runtime


def _set_json(ctx: click.Context, _param: click.Parameter, value: bool) -> None:
    if value:
        runtime_of(ctx).json_mode = True


def _set_api(ctx: click.Context, _param: click.Parameter, value: str) -> None:
    if value:
        runtime_of(ctx).api_url = value


def as_uuid(value: Any) -> Optional[str]:
    """*value* as a canonical UUID string, or None."""
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        return None


def uuid_argument(ctx: click.Context, param: click.Parameter, value: str) -> str:
    """An id argument is a full UUID; the probes' eight-character habit is a
    usage error here, not a 422 from the API."""
    canonical = as_uuid(value)
    if canonical is None:
        raise click.BadParameter(
            f"{param.human_readable_name.lower()} is a full UUID", ctx=ctx, param=param
        )
    return canonical


def global_options(command: Callable[..., Any]) -> Callable[..., Any]:
    """``--json`` and ``--api URL`` on a group or a verb."""
    command = click.option(
        "--api",
        "api_url",
        metavar="URL",
        expose_value=False,
        callback=_set_api,
        help=f"The API to talk to (default {DEFAULT_API_URL}; also ${API_URL_ENV}).",
    )(command)
    command = click.option(
        "--json",
        "json_mode",
        is_flag=True,
        expose_value=False,
        callback=_set_json,
        help="Print one JSON envelope instead of text.",
    )(command)
    return command
