"""The environment verbs: ``health``, ``deploys``, ``webhook`` and ``doctor``
(phase 03 of the v2 CLI).

Each prints a REPORT and returns a VERDICT: the report is the envelope's
``data`` (so an agent reads the facts whatever the verdict), and the exit
code says whether the facts are good — 0, or the code of what is wrong (4
for the API or its deployment, 5 for Railway, 3 for the token, 64 for the
local configuration). ``health`` reads the API's three health surfaces;
``deploys`` reads Railway through the one seam that checks the login and
the linked project first (fork F1 (a), hardened); ``webhook`` is
`scripts/telegram_webhook.py` absorbed; ``doctor`` runs every check a fresh
clone or a broken laptop would fail and names the fix on the same line.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping, Optional

import click

from scripts import posting_monitor, scheduling_monitor
from src.services.target.vocabulary import (
    EXIT_API_UNREACHABLE,
    EXIT_NOT_AUTHORIZED,
    EXIT_OK,
    EXIT_RAILWAY_UNREACHABLE,
    EXIT_USAGE,
    envelope,
)
from storydump_cli import webhook as webhook_tool
from storydump_cli.client import ApiError, Unreachable
from storydump_cli.commands import begin, global_options
from storydump_cli.config import CONFIG_FILE, ConfigError
from storydump_cli.output import emit
from storydump_cli.railway import (
    DEPLOYMENTS_PER_SERVICE,
    DONE_STATUSES,
    ENVIRONMENT,
    FAILED_STATUSES,
    SERVICES,
    Railway,
    RailwayUnavailable,
)
from storydump_cli.storage import TOKEN_ENV, StorageUnavailable
from storydump_cli.watch import Watched, watch

#: `/health` spells its verdict one of these ways.
WELL_WORDS = frozenset({"ok", "healthy"})
DEPLOYS_EVERY = 15.0
_MIGRATION_FILE = re.compile(r"^(\d{3})_.*\.sql$")

#: The two dependency surfaces are judged by the FLEET MONITORS' own `classify`
#: (`scripts/scheduling_monitor.py`, `scripts/posting_monitor.py` — stdlib-only,
#: so the CLI stays a pure client): a surface is not well exactly when the
#: monitor would page on it. A quiet estate with nothing to watch is not an
#: outage, so `no-signal` and `never-posted` inside its grace are well.
SCHEDULING_ALERTS = frozenset(
    {
        scheduling_monitor.STALLED,
        scheduling_monitor.UNREACHABLE,
        scheduling_monitor.WORKER_DOWN,
    }
)
POSTING_ALERTS = frozenset(
    {
        posting_monitor.SILENT,
        posting_monitor.UNREACHABLE,
        posting_monitor.NEVER_POSTED_OVERDUE,
    }
)


# --- health -------------------------------------------------------------------


def surface_verdict(name: str, payload: Any) -> tuple[bool, dict[str, str]]:
    """``(well, {"state", "detail"})`` for one surface: `/health` by its
    `status`; scheduling and posting by the fleet monitors' own verdicts."""
    if not isinstance(payload, dict):
        return False, {"state": "unreachable", "detail": "no answer"}
    if "error" in payload:
        return False, {"state": "unreachable", "detail": str(payload["error"])}
    if name == "api":
        status = payload.get("status")
        well = status is not None and str(status).lower() in WELL_WORDS
        return well, {"state": str(status), "detail": f"/health says {status}"}
    body = json.dumps(payload)
    if name == "scheduling":
        verdict = scheduling_monitor.classify(
            200, body, threshold_s=scheduling_monitor.DEFAULT_STALL_THRESHOLD_S
        )
        return verdict.state not in SCHEDULING_ALERTS, {
            "state": verdict.state,
            "detail": verdict.detail,
        }
    if name == "posting":
        verdict = posting_monitor.classify(
            200,
            body,
            silence_s=posting_monitor.DEFAULT_SILENCE_THRESHOLD_S,
            grace_s=posting_monitor.DEFAULT_GRACE_S,
            watched_s=0.0,
        )
        return verdict.state not in POSTING_ALERTS, {
            "state": verdict.state,
            "detail": verdict.detail,
        }
    return True, {"state": "reported", "detail": ""}


def surface_is_well(name: str, payload: Any) -> bool:
    return surface_verdict(name, payload)[0]


@click.command()
@global_options
@click.pass_context
def health(ctx: click.Context) -> int:
    """The API's three health surfaces — liveness, scheduling, posting — as
    the API reports them, judged by the fleet monitors' own verdicts (the same
    `classify` the pollers run): not well when a monitor would page — a cursor
    stalled past 10 minutes, the worker down, 48 hours of silence, a first post
    overdue past its grace, or a surface unreachable. Exit 0 when every surface
    is well, 4 otherwise — the report is still printed with each verdict; a
    surface that answers 503 is reported as its error.

    \b
    Example:
      storydump health
      storydump health --json
    """
    runtime = begin(ctx, "health")
    surfaces = runtime.client(None).health()
    verdicts = {
        name: surface_verdict(name, payload) for name, payload in surfaces.items()
    }
    ok = all(well for well, _ in verdicts.values())
    data = {"ok": ok, **surfaces, "verdicts": {k: v for k, (_, v) in verdicts.items()}}
    emit(envelope("health", data), json_mode=runtime.json_mode)
    return EXIT_OK if ok else EXIT_API_UNREACHABLE


# --- deploys ------------------------------------------------------------------


def _of_commit(row: dict[str, Any], commit: Optional[str]) -> bool:
    return commit is None or str(row.get("commit") or "").startswith(commit)


def deploys_watched(commit: Optional[str]) -> Watched:
    """How `deploys --watch` is judged. A reading holds each service's LATEST
    deployment only. With *commit*, only rows of that commit count — a watch
    started right after a push waits for the new deployments instead of
    ending on the previous SUCCESS rows — so done is both services' latest
    rows carrying it and terminal, and a failure is that commit failing."""

    def failed(rows: list[dict[str, Any]]) -> Optional[str]:
        bad = [
            row
            for row in rows
            if str(row.get("status")) in FAILED_STATUSES and _of_commit(row, commit)
        ]
        if not bad:
            return None
        return "; ".join(
            f"the latest {row.get('service')} deploy {str(row.get('id'))[:8]}"
            f" ({row.get('commit')}) {row.get('status')}"
            for row in bad
        )

    def done(
        previous: Optional[list[dict[str, Any]]], rows: list[dict[str, Any]]
    ) -> bool:
        latest = {
            row.get("service"): row
            for row in rows
            if isinstance(row.get("service"), str)
        }
        return set(latest) >= set(SERVICES) and all(
            str(row.get("status")) in DONE_STATUSES and _of_commit(row, commit)
            for row in latest.values()
        )

    return Watched(
        "deploys",
        lambda row: row.get("id"),
        failed,
        done,
        scope="service",
        collection="services",
        failed_on_baseline=True,
    )


def _read_deploys(rail: Railway, *, limit: int) -> list[dict[str, Any]]:
    """``[{"service", "rows"}]`` for both services, newest deployment first."""
    return [
        {"service": service, "rows": rail.deployments(service, limit=limit)}
        for service in SERVICES
    ]


@click.command()
@global_options
@click.option(
    "--watch",
    "watching",
    is_flag=True,
    help="Re-read until both services' latest deploys end.",
)
@click.option(
    "--every",
    type=click.FloatRange(min=1),
    default=DEPLOYS_EVERY,
    show_default=True,
    metavar="SECONDS",
    help="Seconds between reads under --watch.",
)
@click.option(
    "--commit",
    "commit",
    default=None,
    metavar="SHA",
    help=(
        "Under --watch, wait for the deployments of this commit (a prefix of the"
        " hash); without it the latest rows decide, whatever they deploy."
    ),
)
@click.pass_context
def deploys(
    ctx: click.Context, watching: bool, every: float, commit: Optional[str]
) -> int:
    """The latest deployments of the API and the worker on Railway's
    production environment, with their commits — through your own `railway`
    login, checked first along with the linked project. --watch ends 0 when
    both latest deploys are SUCCESS (a sleeping or skipped deployment counts)
    and 6 when one FAILED or CRASHED; pass --commit <sha> right after a push
    so the watch waits for that commit's deployments instead of ending on the
    previous ones. A service with no live deployment keeps the watch running
    (Ctrl-C ends it, exit 0).

    \b
    Examples:
      storydump deploys
      storydump deploys --watch --commit 0b0badc
    """
    runtime = begin(ctx, "deploys")
    rail = Railway(runtime.run_process)
    rail.whoami()
    project = rail.linked_project()
    if watching:
        return watch(
            runtime,
            deploys_watched(commit),
            lambda: _read_deploys(rail, limit=1),
            every=every,
        )
    data = {
        "railway_version": rail.version(),
        "project": project,
        "environment": ENVIRONMENT,
        "services": _read_deploys(rail, limit=DEPLOYMENTS_PER_SERVICE),
    }
    emit(envelope("deploys", data), json_mode=runtime.json_mode)
    return EXIT_OK


# --- webhook ------------------------------------------------------------------


def _url_option(command):
    return click.option(
        "--url",
        default=None,
        metavar="URL",
        help=(
            "The API's webhook door, https only (default the deployment's"
            f" {webhook_tool.WEBHOOK_URL_VAR}, else {webhook_tool.DEFAULT_WEBHOOK_URL})."
        ),
    )(command)


@click.group()
def webhook() -> None:
    """Check, register or remove the bot's Telegram webhook — the variables the
    deployment uses, read from this shell; no secret is ever printed.

    \b
    Examples:
      storydump webhook status
      storydump webhook register
    """


def _report(runtime: Any, data: dict[str, Any]) -> int:
    emit(envelope("webhook", data), json_mode=runtime.json_mode)
    return webhook_tool.exit_code(data)


@webhook.command("status")
@global_options
@_url_option
@click.pass_context
def webhook_status(ctx: click.Context, url: Optional[str]) -> int:
    """Who is the bot, is a webhook set, does the API accept the secret.
    Exit 0 when every check passes, 4 when one fails.

    \b
    Example:
      storydump webhook status
    """
    runtime = begin(ctx, "webhook")
    return _report(runtime, webhook_tool.status(runtime.env, url=url))


@webhook.command("register")
@global_options
@_url_option
@click.option(
    "--drop-pending",
    "drop_pending",
    is_flag=True,
    help=(
        "Discard updates Telegram queued before now (first arming of a new bot;"
        " NOT for re-registering a live one — real taps would be lost)."
    ),
)
@click.pass_context
def webhook_register(ctx: click.Context, url: Optional[str], drop_pending: bool) -> int:
    """setWebhook → the API with the secret and the served update kinds;
    refuses unless the token's bot is the configured bot; then the status
    checks.

    \b
    Example:
      storydump webhook register
    """
    runtime = begin(ctx, "webhook")
    return _report(
        runtime, webhook_tool.register(runtime.env, url=url, drop_pending=drop_pending)
    )


@webhook.command("deregister")
@global_options
@click.pass_context
def webhook_deregister(ctx: click.Context) -> int:
    """deleteWebhook: Telegram delivers nothing until `register` runs again.

    \b
    Example:
      storydump webhook deregister
    """
    runtime = begin(ctx, "webhook")
    return _report(runtime, webhook_tool.deregister(runtime.env))


# --- doctor -------------------------------------------------------------------

#: The exit code each check answers with when it is not ok, in report order.
CHECK_CODES: Mapping[str, int] = {
    "token": EXIT_NOT_AUTHORIZED,
    "api": EXIT_API_UNREACHABLE,
    "storage": EXIT_NOT_AUTHORIZED,
    "config": EXIT_USAGE,
    "railway": EXIT_RAILWAY_UNREACHABLE,
    "ledger": EXIT_API_UNREACHABLE,
}


def _role_text(role: Any) -> str:
    """`/health.db_role`: a name, or `{user, bypassrls}` since phase 02."""
    if isinstance(role, dict):
        bypass = "bypasses RLS" if role.get("bypassrls") else "under RLS"
        return f"{role.get('user')} ({bypass})"
    return str(role) if role else "unknown"


def _versions_in(directory: Path) -> Optional[set[int]]:
    """The migration versions in a checkout's ``scripts/migrations``, or None
    outside one."""
    if not directory.is_dir():
        return None
    versions = set()
    for path in directory.iterdir():
        match = _MIGRATION_FILE.match(path.name)
        if match:
            versions.add(int(match.group(1)))
    return versions


def _storage_name(runtime: Any) -> str:
    if (runtime.env.get(TOKEN_ENV) or "").strip():
        return f"env ({TOKEN_ENV})"
    try:
        storage = runtime.config().token_storage
    except ConfigError:
        storage = "keychain"
    if storage == "file":
        return "file (0600, under the config directory)"
    return type(runtime.backend()).__name__.replace("Backend", "").lower()


@click.command()
@global_options
@click.pass_context
def doctor(ctx: click.Context) -> int:
    """Check the token, the API, the token store, the local configuration,
    the Railway login and link, and the migration ledger against this
    checkout; each line ok, wrong, missing or skipped, with its fix. Exit 0
    when everything is ok, else the first wrong check's own code.

    \b
    Example:
      storydump doctor
    """
    runtime = begin(ctx, "doctor")
    checks: dict[str, tuple[str, str, str]] = {}

    # --- the API first: the token can only be checked through it ---------------
    api_client = runtime.client(None)
    api_up = False
    try:
        health_payload = api_client.health_api()
        api_up = True
        checks["api"] = (
            "ok",
            f"{runtime.api_url} — version {health_payload.get('version')},"
            f" role {_role_text(health_payload.get('db_role'))}",
            "",
        )
    except Unreachable as exc:
        checks["api"] = (
            "missing",
            f"unreachable: {exc.detail}",
            "check STORYDUMP_API and the network",
        )
    except ApiError as exc:
        checks["api"] = (
            "wrong",
            f"{runtime.api_url} answered {exc.status}: {exc.detail}",
            "check STORYDUMP_API",
        )

    # --- the token -----------------------------------------------------------------
    token: Optional[str] = None
    principal: Optional[dict[str, Any]] = None
    try:
        token = runtime.token()
    except ConfigError:
        checks["token"] = (
            "skipped",
            "the config file could not be read",
            "fix the config file",
        )
    except StorageUnavailable as exc:
        checks["token"] = ("missing", exc.detail, exc.fix)
    else:
        if not token:
            checks["token"] = (
                "missing",
                "no token stored",
                f"run storydump login (or set {TOKEN_ENV})",
            )
        elif not api_up:
            checks["token"] = (
                "skipped",
                "could not be checked: the API is unreachable",
                "",
            )
        else:
            try:
                principal = runtime.client(token).principal()
            except ApiError as exc:
                checks["token"] = (
                    "wrong",
                    f"the API refuses it ({exc.reason or exc.status})",
                    "run storydump login with a token minted on the web under Settings › API tokens",
                )
            except Unreachable as exc:
                checks["token"] = ("skipped", f"could not be checked: {exc.detail}", "")
            else:
                facts = (
                    principal.get("token")
                    if isinstance(principal.get("token"), dict)
                    else {}
                )
                checks["token"] = (
                    "ok",
                    f"{facts.get('name')} ({facts.get('role')}, expires"
                    f" {str(facts.get('expires_at') or '?')[:10]})",
                    "",
                )

    # --- the store and the config ----------------------------------------------------
    try:
        checks["storage"] = ("ok", _storage_name(runtime), "")
    except StorageUnavailable as exc:
        checks["storage"] = ("missing", exc.detail, exc.fix)
    try:
        config = runtime.config()
        path = runtime.config_dir / CONFIG_FILE
        checks["config"] = (
            "ok",
            f"{path} — api {config.api_url or runtime.api_url}, storage {config.token_storage}"
            if path.exists()
            else f"defaults (no {path})",
            "",
        )
    except ConfigError as exc:
        checks["config"] = (
            "wrong",
            str(exc),
            "fix the config file, or delete it and run storydump login again",
        )

    # --- railway ---------------------------------------------------------------------
    rail = Railway(runtime.run_process)
    try:
        version = rail.version()
        rail.whoami()
        project = rail.linked_project()
        checks["railway"] = (
            "ok",
            f"railway {version} — logged in — project {project['name']}",
            "",
        )
    except RailwayUnavailable as exc:
        # not installed or not logged in is MISSING (the plan's checklist);
        # another project, or a binary answering badly, is WRONG
        missing = "not installed" in exc.detail or "not logged in" in exc.detail
        checks["railway"] = ("missing" if missing else "wrong", exc.detail, exc.fix)

    # --- the ledger against the checkout -----------------------------------------------
    if principal is None or token is None:
        checks["ledger"] = ("skipped", "needs a valid token and a reachable API", "")
    else:
        try:
            posture = runtime.client(token).ops_posture()
        except (ApiError, Unreachable) as exc:
            checks["ledger"] = ("skipped", f"could not read posture: {exc.detail}", "")
        else:
            data = posture.get("data") if isinstance(posture, dict) else None
            migrations = (
                (data or {}).get("migrations") if isinstance(data, dict) else None
            )
            applied = {
                int(m["version"])
                for m in (migrations or [])
                if isinstance(m, dict) and str(m.get("version", "")).isdigit()
            }
            latest = f"{max(applied):03d}" if applied else "none"
            repo = _versions_in(runtime.migrations_dir)
            if repo is None:
                checks["ledger"] = (
                    "skipped",
                    f"not a repository checkout; the API reports {len(applied)} migrations"
                    f" (latest {latest})",
                    "",
                )
            else:
                missing = sorted(repo - applied)
                extra = sorted(applied - repo)
                if missing:
                    checks["ledger"] = (
                        "wrong",
                        f"{len(missing)} migration(s) in the checkout not applied:"
                        f" {', '.join(f'{v:03d}' for v in missing)} (applied through {latest})",
                        "deploy main (the worker's pre-deploy runner applies migrations)",
                    )
                elif extra:
                    checks["ledger"] = (
                        "wrong",
                        f"{len(extra)} migration(s) applied that this checkout lacks:"
                        f" {', '.join(f'{v:03d}' for v in extra)}",
                        "git pull — the checkout is behind the deployment",
                    )
                else:
                    checks["ledger"] = (
                        "ok",
                        f"{len(applied)} migrations applied, latest {latest}, matching the checkout",
                        "",
                    )

    ordered = [
        {"check": name, "state": state, "value": value, "fix": fix}
        for name, (state, value, fix) in (
            (name, checks[name])
            for name in ("token", "api", "storage", "config", "railway", "ledger")
        )
    ]
    ok = all(row["state"] in ("ok", "skipped") for row in ordered)
    emit(envelope("doctor", {"ok": ok, "checks": ordered}), json_mode=runtime.json_mode)
    for row in ordered:
        if row["state"] not in ("ok", "skipped"):
            return CHECK_CODES[row["check"]]
    return EXIT_OK


COMMANDS = (health, deploys, webhook, doctor)
