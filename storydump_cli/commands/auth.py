"""``login``, ``logout``, ``whoami`` and ``tokens list|revoke``.

The token never travels on argv: ``login`` reads it from a hidden prompt on
a terminal and from stdin otherwise (``echo "$TOKEN" | storydump login`` is
how an agent does it), checks its shape before spending a request on it,
proves it against ``GET /me/principal`` and only then stores it. There is
no ``mint``: a token is minted once, on the web, from a signed-in session,
and shown once — a token cannot mint a token (spec §2).
"""

from __future__ import annotations

import sys
from typing import Any

import click

from src.services.target.vocabulary import (
    EXIT_API_UNREACHABLE,
    EXIT_NOT_AUTHORIZED,
    EXIT_NOT_FOUND,
    TOKEN_PREFIX,
    envelope,
)
from storydump_cli.client import ApiError, Client
from storydump_cli.commands import begin, global_options
from storydump_cli.config import Config, write_config
from storydump_cli.output import Failure, emit
from storydump_cli.storage import (
    TOKEN_ENV,
    EnvBackend,
    FileBackend,
    StorageUnavailable,
    token_path,
)

MINT_HINT = "mint one on the web under Settings › API tokens"


def signed_in_client(runtime: Any) -> Client:
    """A client carrying the stored token, or the answer that there is none."""
    token = runtime.token()
    if not token:
        raise Failure(
            code=EXIT_NOT_AUTHORIZED,
            reason="not_authorized",
            detail="not signed in",
            fix=f"run storydump login, or set {TOKEN_ENV}",
        )
    return runtime.client(token)


def _read_secret(runtime: Any) -> str:
    if runtime.stdin_is_tty:
        return str(
            click.prompt("API token", hide_input=True, default="", show_default=False)
        ).strip()
    stream = sys.stdin
    return (stream.readline() if stream is not None else "").strip()


def _workspace_of(principal: dict[str, Any]) -> Any:
    """A service identity's workspace; ``None`` for a person-bound token."""
    token = principal.get("token") or {}
    return token.get("workspace_id") if isinstance(token, dict) else None


def _list_tokens(client: Client, principal: dict[str, Any]) -> dict[str, Any]:
    workspace_id = _workspace_of(principal)
    if workspace_id:
        return client.list_workspace_tokens(workspace_id)
    return client.list_my_tokens()


def _no_such_token() -> Failure:
    return Failure(
        code=EXIT_NOT_FOUND,
        reason="not_found",
        detail="no such token",
        fix="run storydump tokens list for the ids this token can see",
    )


@click.command()
@global_options
@click.option(
    "--insecure-storage",
    is_flag=True,
    help="Keep the token in a 0600 file under the config directory instead of the OS keychain.",
)
@click.pass_context
def login(ctx: click.Context, insecure_storage: bool) -> None:
    """Store an API token after proving it against the API.

    Paste the token at the prompt, or pipe it in. It goes to the OS keychain;
    where there is none, --insecure-storage keeps it in a 0600 file instead.

    \b
    Examples:
      storydump login
      echo "$STORYDUMP_TOKEN" | storydump login --insecure-storage
    """
    runtime = begin(ctx, "login")
    if EnvBackend(runtime.env).get():
        raise click.UsageError(
            f"{TOKEN_ENV} is set and wins over any stored token — unset it to sign in"
        )
    secret = _read_secret(runtime)
    if not secret:
        raise click.UsageError(
            "no token given — paste it at the prompt, or pipe it on stdin"
        )
    if not secret.startswith(TOKEN_PREFIX):
        raise click.UsageError(f"that is not a storydump token — {MINT_HINT}")
    try:
        principal = runtime.client(secret).principal()
    except ApiError as exc:
        if exc.status in (401, 403):
            raise Failure(
                code=EXIT_NOT_AUTHORIZED,
                reason="not_authorized",
                detail="the API refused that token — it may be expired or revoked",
                fix=MINT_HINT,
            ) from exc
        raise Failure(
            code=EXIT_API_UNREACHABLE,
            reason="api_unreachable",
            detail=f"the API at {runtime.api_url} did not answer /me/principal"
            f" as expected ({exc.status})",
            fix="check --api / STORYDUMP_API points at the storydump API",
        ) from exc
    file_store = FileBackend(token_path(runtime.config_dir))
    if insecure_storage:
        file_store.set(secret)
        storage = "file"
        try:  # one stored token, not two: an earlier keychain login goes
            runtime.backend().delete()
        except StorageUnavailable:
            pass
    else:
        runtime.backend().set(secret)
        file_store.delete()  # one stored token, not two
        storage = "keychain"
    write_config(
        runtime.config_dir, Config(api_url=runtime.api_url, token_storage=storage)
    )
    emit(envelope("login", principal), json_mode=runtime.json_mode)


@click.command()
@global_options
@click.pass_context
def logout(ctx: click.Context) -> None:
    """Forget the stored token: the keychain entry and the file, whichever exist.

    \b
    Example:
      storydump logout
    """
    runtime = begin(ctx, "logout")
    FileBackend(token_path(runtime.config_dir)).delete()
    if runtime.config().token_storage == "keychain":
        runtime.backend().delete()
    else:
        try:
            runtime.backend().delete()
        except StorageUnavailable:
            pass  # no keychain here, so nothing of ours is in one
    data: dict[str, Any] = {"signed_out": True}
    if EnvBackend(runtime.env).get():
        data["note"] = f"{TOKEN_ENV} is still set in this shell"
    emit(envelope("logout", data), json_mode=runtime.json_mode)


@click.command()
@global_options
@click.pass_context
def whoami(ctx: click.Context) -> None:
    """Show the principal the stored token resolves to.

    The kind, the token's name, role and expiry, and one line per workspace
    with your role there.

    \b
    Example:
      storydump whoami --json
    """
    runtime = begin(ctx, "whoami")
    principal = signed_in_client(runtime).principal()
    emit(envelope("whoami", principal), json_mode=runtime.json_mode)


@click.group()
@global_options
def tokens() -> None:
    """List and revoke the tokens this principal can see.

    A person sees their own tokens; a service identity sees its workspace's.
    Minting is on the web (Settings › API tokens), never here.

    \b
    Example:
      storydump tokens list
    """


@tokens.command("list")
@global_options
@click.pass_context
def tokens_list(ctx: click.Context) -> None:
    """List tokens: id, name, role, expiry, last use, and whether revoked.

    \b
    Example:
      storydump tokens list --json
    """
    runtime = begin(ctx, "tokens")
    client = signed_in_client(runtime)
    rows = _list_tokens(client, client.principal())
    emit(envelope("tokens", rows), json_mode=runtime.json_mode)


@tokens.command("revoke")
@global_options
@click.argument("token_id")
@click.pass_context
def tokens_revoke(ctx: click.Context, token_id: str) -> None:
    """Revoke one token by its id (from `tokens list`); it stops working at once.

    \b
    Example:
      storydump tokens revoke 3f1c0b2e-8d4a-4f6b-9c1e-2a7d5e8b9c0d
    """
    runtime = begin(ctx, "tokens")
    client = signed_in_client(runtime)
    principal = client.principal()
    rows = _list_tokens(client, principal).get("tokens") or []
    row = next(
        (r for r in rows if isinstance(r, dict) and r.get("id") == token_id), None
    )
    if row is None:
        raise _no_such_token()
    workspace_id = _workspace_of(principal)
    try:
        if workspace_id:
            client.revoke_workspace_token(workspace_id, token_id)
        else:
            client.revoke_my_token(token_id)
    except ApiError as exc:
        if exc.status == 404:
            raise _no_such_token() from exc
        raise
    emit(
        envelope("tokens", {"revoked": {"id": token_id, "name": row.get("name")}}),
        json_mode=runtime.json_mode,
    )


COMMANDS = (login, logout, whoami, tokens)
