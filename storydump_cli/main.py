"""The console script: a Click group whose exit codes are the CLI's own.

Click's ``main`` prints a usage error and exits 2; the spec (§6) says 64,
and says a refusal or an authorization failure is an answer with a
documented code and one envelope, never a traceback. So the group's
``main`` runs Click with ``standalone_mode=False`` and maps what comes
back — a usage error, an ``ApiError``, a store that cannot be used — onto
one envelope and one exit code; ``main()`` below is the console script.

``Runtime`` is everything a verb needs: where the API is, where the token
lives, how to reach the network, how to print. Tests build one by hand
(a ``MemoryBackend``, an ``httpx.MockTransport``) and pass it as ``obj``;
``build_runtime`` builds the real one from the environment and the config
file. The keychain backend is built at first use, not here, so ``--api``
has been seen by the time the keychain entry's name (the API host) is
decided.
"""

from __future__ import annotations

import io
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlparse

import click
import httpx

from src.services.target import vocabulary
from storydump_cli import __version__
from storydump_cli.client import ApiError, Client, Unreachable, InsecureApiUrl
from storydump_cli.commands import auth, global_options, reads
from storydump_cli.config import (
    API_URL_ENV,
    DEFAULT_API_URL,
    Config,
    ConfigError,
    config_dir,
    read_config,
)
from storydump_cli.output import Failure, emit, redact
from storydump_cli.storage import (
    Backend,
    KeychainBackend,
    StorageUnavailable,
    resolve_token,
)

PROG = "storydump"
UNREACHABLE_FIX = "check STORYDUMP_API and the network"

#: What fixes each authorization answer, by the vocabulary's reason.
FIXES: Mapping[str, str] = {
    "not_authorized": "run storydump login with a token minted on the web under Settings › API tokens",
    "session_required": "do this signed in on the web; a token cannot",
    "readonly_token": "use a token minted with the operator role",
    "wrong_workspace": "use a token minted for that workspace, or a person-bound token",
    "not_a_member": "check the workspace id, and this token's workspaces with storydump whoami",
}
INSECURE_HTTP_ENV = "STORYDUMP_INSECURE_HTTP"
DEFAULT_FIX = "see storydump --help"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Runtime:
    config_dir: Path
    api_url: str
    token_backend: Optional[Backend] = None
    transport: Optional[httpx.BaseTransport] = None
    json_mode: bool = False
    stdin_is_tty: bool = False
    env: Mapping[str, str] = field(default_factory=dict)
    #: The verb in flight, for the envelope of a failure it never saw.
    kind: Optional[str] = None
    #: The clock and the sleeper — ``--since`` is measured from the clock and
    #: ``--watch`` waits on the sleeper — so a test runs a scripted watch
    #: instantly with a fixed ``now``.
    now_fn: Callable[[], datetime] = utc_now
    sleep_fn: Callable[[float], None] = time.sleep

    @property
    def api_host(self) -> str:
        parsed = urlparse(self.api_url)
        host = parsed.hostname or self.api_url
        return f"{host}:{parsed.port}" if parsed.port else host

    def backend(self) -> Backend:
        """The secure store: the OS keychain keyed by the API host, unless a
        test handed in another."""
        if self.token_backend is None:
            self.token_backend = KeychainBackend(self.api_host)
        return self.token_backend

    def config(self) -> Config:
        return read_config(self.config_dir)

    def token(self) -> Optional[str]:
        """``STORYDUMP_TOKEN``, else the store the config names."""
        return resolve_token(
            env=self.env,
            config_dir=self.config_dir,
            token_storage=self.config().token_storage,
            keychain=self.backend(),
        )

    def client(self, token: Optional[str]) -> Client:
        return Client(
            self.api_url,
            token,
            transport=self.transport,
            allow_insecure_http=self.env.get(INSECURE_HTTP_ENV, "").strip().lower()
            in ("1", "true", "yes"),
        )


def build_runtime(env: Mapping[str, str] = os.environ) -> Runtime:
    """The real runtime: ``--api`` beats ``$STORYDUMP_API`` beats the config
    file beats the default (the flag is applied later, by its callback)."""
    directory = config_dir(env)
    api_url = env.get(API_URL_ENV) or read_config(directory).api_url or DEFAULT_API_URL
    stdin = sys.stdin
    return Runtime(
        config_dir=directory,
        api_url=api_url,
        env=env,
        stdin_is_tty=bool(stdin is not None and stdin.isatty()),
    )


def _report(runtime: Runtime, *, code: int, reason: str, detail: str, fix: str) -> int:
    emit(
        vocabulary.error_envelope(
            runtime.kind or "error", code=code, reason=reason, detail=detail, fix=fix
        ),
        json_mode=runtime.json_mode,
    )
    return code


def _usage(runtime: Runtime, exc: click.ClickException) -> int:
    """A usage error: Click's own text on stderr — redacted, in case a bad
    value is echoed — or, once ``--json`` has been seen, an envelope."""
    if runtime.json_mode:
        return _report(
            runtime,
            code=vocabulary.EXIT_USAGE,
            reason="usage",
            detail=exc.format_message(),
            fix=f"run {PROG} --help",
        )
    buffer = io.StringIO()
    exc.show(file=buffer)
    sys.stderr.write(redact(buffer.getvalue()))
    sys.stderr.flush()
    return vocabulary.EXIT_USAGE


def _reason_of(exc: ApiError) -> str:
    """The vocabulary reason for an answer: the body's when it has one, else
    what the status means on its own (a reason-less 404 is a workspace the
    principal cannot see — an authorization answer)."""
    if exc.reason:
        return exc.reason
    if exc.status in (401, 403):
        return "not_authorized"
    if exc.status == 404:
        return "not_a_member"
    return "refused"


def _config_error(runtime: Runtime, exc: ConfigError) -> int:
    fix = "fix the config file, or delete it and run storydump login again"
    if isinstance(exc, InsecureApiUrl):
        fix = (
            "use an https URL for --api / STORYDUMP_API, or set"
            f" {INSECURE_HTTP_ENV}=1 for a development server"
        )
    return _report(
        runtime,
        code=vocabulary.EXIT_USAGE,
        reason="usage",
        detail=str(exc),
        fix=fix,
    )


def dispatch(
    group: click.Group, args: Any, prog_name: Optional[str], extra: dict[str, Any]
) -> int:
    """Run *group* with the runtime in *extra* (built here when a test did
    not hand one in) and turn whatever it raised into an exit code."""
    runtime: Optional[Runtime] = extra.get("obj")
    if runtime is None:
        try:
            runtime = build_runtime()
        except ConfigError as exc:
            return _config_error(Runtime(config_dir(os.environ), DEFAULT_API_URL), exc)
        extra["obj"] = runtime
    try:
        rv = click.Group.main(
            group,
            args=args,
            prog_name=prog_name or PROG,
            standalone_mode=False,
            **extra,
        )
        return rv if isinstance(rv, int) else vocabulary.EXIT_OK
    except click.Abort:
        return vocabulary.EXIT_USAGE
    except click.ClickException as exc:
        return _usage(runtime, exc)
    except ConfigError as exc:
        return _config_error(runtime, exc)
    except Failure as exc:
        return _report(
            runtime, code=exc.code, reason=exc.reason, detail=exc.detail, fix=exc.fix
        )
    except Unreachable as exc:
        if exc.status == 0:
            detail = f"could not reach {runtime.api_url}: {exc.detail}"
        else:
            detail = f"{runtime.api_url} answered {exc.status}: {exc.detail}"
        return _report(
            runtime,
            code=vocabulary.EXIT_API_UNREACHABLE,
            reason="api_unreachable",
            detail=detail,
            fix=UNREACHABLE_FIX,
        )
    except ApiError as exc:
        reason = _reason_of(exc)
        return _report(
            runtime,
            code=vocabulary.exit_code_for(exc.status, exc.reason),
            reason=reason,
            detail=vocabulary.REASON_SENTENCES.get(reason, exc.detail),
            fix=FIXES.get(reason, DEFAULT_FIX),
        )
    except StorageUnavailable as exc:
        return _report(
            runtime,
            code=vocabulary.EXIT_NOT_AUTHORIZED,
            reason="storage_unavailable",
            detail=exc.detail,
            fix=exc.fix,
        )


class Cli(click.Group):
    """The root group. Its ``main`` owns the exit codes: in standalone mode
    it exits with the CLI's code, never Click's; otherwise it returns it."""

    def main(  # type: ignore[override]
        self,
        args: Any = None,
        prog_name: Optional[str] = None,
        complete_var: Optional[str] = None,
        standalone_mode: bool = True,
        **extra: Any,
    ) -> int:
        if complete_var is not None:
            extra["complete_var"] = complete_var
        code = dispatch(self, args, prog_name, extra)
        if standalone_mode:
            sys.exit(code)
        return code


@click.group(
    cls=Cli, name=PROG, context_settings={"help_option_names": ["-h", "--help"]}
)
@global_options
@click.version_option(__version__, prog_name=PROG)
def cli() -> None:
    """storydump — the operator's terminal for the storydump API.

    Mint a token on the web (Settings › API tokens), then:

    \b
      storydump login
      storydump whoami
      storydump floating --watch
      storydump story <id> --json
      storydump burst --since 3h

    A read answers for every workspace the token can see unless --workspace
    names one. --watch re-reads on an interval and prints only what changed:
    it ends 0 on the verb's terminal condition (floating drained, a burst
    with nothing mid-flight) or on Ctrl-C, and 6 when a read shows the
    verb's failure condition.

    \b
    Exit codes:
      0  ok            3  not authorized       6  a watched condition failed
      1  not found     4  API unreachable     64  usage
      2  refused       5  Railway unreachable
    """


for command in (*auth.COMMANDS, *reads.COMMANDS):
    cli.add_command(command)


def main(argv: Optional[list[str]] = None) -> int:
    """The console script: the CLI's exit code, for ``sys.exit``."""
    return cli.main(args=argv, prog_name=PROG, standalone_mode=False)


if __name__ == "__main__":
    sys.exit(main())
