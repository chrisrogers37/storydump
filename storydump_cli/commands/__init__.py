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
from typing import Any, Callable, Optional

import click

from storydump_cli.config import API_URL_ENV, DEFAULT_API_URL


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
