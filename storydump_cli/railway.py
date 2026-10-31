"""The one seam to the ``railway`` binary: the
developer's own Railway credential, read through the CLI Railway ships.

Nothing here is trusted before it is checked: ``whoami`` must succeed (a
logged-out account is exit 5 with the sentence that fixes it), the linked
project must be THIS repository's (another session's ``railway login`` is
known to drop or swap the link), and only then is a deployment read. Every
call goes through one ``run`` callable so a test scripts the binary's
answers; the JSON parsers are written against
:data:`TESTED_VERSION`, and the fixture they are tested with is stamped
with it. The binary holds the credential. The one read that carries secrets,
a service's variables (:meth:`Railway.variables`), reduces each value to a
fingerprint as it is parsed: no value leaves it, and no failure repeats what
the binary printed.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence

from src.services.target.vocabulary import (
    EXIT_RAILWAY_UNREACHABLE,
    RAILWAY_PROJECT_ID_VAR,
    RAILWAY_PROJECT_NAME,
)
from storydump_cli.output import Failure

#: The `railway` version the parsers were written against; the fixture
#: `tests/storydump_cli/fixtures/railway_<version>.json` is captured from it
#: and `doctor` reports the version it finds.
TESTED_VERSION = "4.30.3"
#: The repository's project on Railway: its name, and the variable that holds
#: its id. A link to any other project is refused before a deployment is read.
PROJECT_NAME = RAILWAY_PROJECT_NAME
PROJECT_ID_ENV = RAILWAY_PROJECT_ID_VAR
#: The services `main` deploys — the API and the worker — in display order.
SERVICES: tuple[str, ...] = ("storydump", "worker")
#: How many deployments per service a read lists.
DEPLOYMENTS_PER_SERVICE = 5
TIMEOUT_S = 60

INSTALL_FIX = (
    "install the Railway CLI (https://docs.railway.com/guides/cli), then run"
    " railway login"
)
LOGIN_FIX = f"run railway login, then railway link and pick the {PROJECT_NAME} project"
LINK_FIX = f"run railway link and pick the {PROJECT_NAME} project"
ID_FIX = (
    f"set {PROJECT_ID_ENV} to the id of the {PROJECT_NAME} project"
    " (the `id` in `railway status --json`)"
)

#: The environment `main` deploys to; every deployment read names it, so a
#: directory linked to another environment cannot be read as production.
ENVIRONMENT = "production"
#: A deployment's terminal statuses, as Railway spells them: a SLEEPING
#: deployment is a successful one asleep, a SKIPPED one had nothing to build.
DONE_STATUSES: tuple[str, ...] = ("SUCCESS", "SLEEPING", "SKIPPED")
#: ... and a REMOVED one was taken down: waiting for it to succeed is waiting
#: forever, so a watch reads it as that deployment failing.
FAILED_STATUSES: tuple[str, ...] = ("FAILED", "CRASHED", "REMOVED")

ProcessResult = tuple[int, str, str]
RunProcess = Callable[[Sequence[str]], ProcessResult]

_VERSION = re.compile(r"(\d+\.\d+\.\d+)")


class RailwayUnavailable(Failure):
    """Railway cannot be read: the binary is missing, the account is logged
    out, another project is linked, or the binary answered badly — exit 5.

    One that answers an exception is raised after that exception's handler,
    never inside it: the exception can hold what the binary printed (a
    timeout's output so far, an answer that is not UTF-8, a document that is
    not JSON), and a raise inside the handler keeps it on ``__context__``."""

    def __init__(self, detail: str, fix: str) -> None:
        super().__init__(
            code=EXIT_RAILWAY_UNREACHABLE,
            reason="railway_unreachable",
            detail=detail,
            fix=fix,
        )


def run_railway(args: Sequence[str]) -> ProcessResult:
    """The real binary: ``railway <args>`` → (exit code, stdout, stderr),
    decoded as UTF-8 whatever the locale. ``FileNotFoundError`` when there is
    no binary — the caller says so."""
    proc = subprocess.run(
        ["railway", *args], capture_output=True, encoding="utf-8", timeout=TIMEOUT_S
    )
    return proc.returncode, proc.stdout, proc.stderr


def json_from(out: str) -> Any:
    """The JSON in a command's stdout, skipping any notice printed before it
    (the binary announces updates on stdout)."""
    text = out or ""
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        raise ValueError("no JSON")
    return json.loads(text[min(starts) :])


def parse_version(text: str) -> str:
    """``railway 4.30.3`` → ``4.30.3``."""
    match = _VERSION.search(text or "")
    return match.group(1) if match else (text or "").strip()


def short_hash(value: Any) -> Optional[str]:
    return str(value)[:7] if isinstance(value, str) and value else None


def first_line(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().splitlines()[0]


def deployment_row(service: str, deployment: dict[str, Any]) -> dict[str, Any]:
    """One deployment as the CLI shows it: status, when, which commit."""
    meta = deployment.get("meta") if isinstance(deployment.get("meta"), dict) else {}
    full = meta.get("commitHash")
    return {
        "id": deployment.get("id"),
        "service": service,
        "status": deployment.get("status"),
        "created_at": deployment.get("createdAt"),
        "commit": short_hash(full),
        # the whole hash too: `--commit` is matched against it, so the 40
        # characters a push prints match as well as the seven shown
        "commit_hash": full if isinstance(full, str) and full else None,
        "branch": meta.get("branch"),
        "message": first_line(meta.get("commitMessage")),
    }


@dataclass
class Railway:
    """The binary behind one ``run`` callable, and the id of the project a link
    must carry (:data:`PROJECT_ID_ENV`'s value; without one every link is
    refused)."""

    run: RunProcess
    project_id: Optional[str] = None

    @classmethod
    def from_env(cls, run: RunProcess, env: Mapping[str, str]) -> "Railway":
        """The seam for a process whose environment is *env*."""
        return cls(run, project_id=env.get(PROJECT_ID_ENV))

    def _call(self, *args: str) -> ProcessResult:
        # refused after the handler (see RailwayUnavailable)
        try:
            return self.run(list(args))
        except FileNotFoundError:
            detail, fix = "the railway binary is not installed", INSTALL_FIX
        except subprocess.TimeoutExpired:
            detail, fix = f"railway did not answer within {TIMEOUT_S} s", LOGIN_FIX
        except UnicodeDecodeError:
            detail, fix = "railway answered output that is not UTF-8", LOGIN_FIX
        except OSError as exc:
            detail, fix = f"could not run railway: {type(exc).__name__}", INSTALL_FIX
        raise RailwayUnavailable(detail, fix)

    def version(self) -> str:
        code, out, err = self._call("--version")
        if code != 0:
            raise RailwayUnavailable("railway --version did not answer", INSTALL_FIX)
        return parse_version(out or err)

    def whoami(self) -> str:
        """The logged-in account's line, or exit 5 with ``railway login``."""
        code, out, err = self._call("whoami")
        text = f"{out}\n{err}"
        if (
            code != 0
            or "unauthorized" in text.lower()
            or "not logged in" in text.lower()
        ):
            raise RailwayUnavailable("not logged in to Railway", LOGIN_FIX)
        return (first_line(out) or "logged in").strip()

    def status(self) -> dict[str, Any]:
        code, out, err = self._call("status", "--json")
        if code != 0 or not out.strip():
            raise RailwayUnavailable(
                "no Railway project is linked in this directory", LINK_FIX
            )
        try:
            data = json_from(out)
        except ValueError:
            raise RailwayUnavailable(
                "railway status answered something that is not JSON", LINK_FIX
            )
        if not isinstance(data, dict):
            raise RailwayUnavailable("railway status answered no project", LINK_FIX)
        return data

    def linked_project(self) -> dict[str, Any]:
        """``{"name", "id"}`` of the linked project — refused unless it is
        this repository's: its name, and the id this seam was given."""
        status = self.status()
        name, project_id = status.get("name"), status.get("id")
        expected = (self.project_id or "").strip()
        if name == PROJECT_NAME and not expected:
            raise RailwayUnavailable(
                f"{PROJECT_ID_ENV} is not set, so the linked project"
                f" ({project_id}) cannot be checked",
                ID_FIX,
            )
        if name != PROJECT_NAME or project_id != expected:
            raise RailwayUnavailable(
                f"the linked Railway project is {name!r} ({project_id}), not"
                f" {PROJECT_NAME}",
                LINK_FIX,
            )
        return {"name": name, "id": project_id}

    def deployments(
        self, service: str, *, limit: int = DEPLOYMENTS_PER_SERVICE
    ) -> list[dict[str, Any]]:
        """The latest deployments of *service*, newest first."""
        code, out, err = self._call(
            "deployment",
            "list",
            "--service",
            service,
            "--environment",
            ENVIRONMENT,
            "--json",
        )
        if code != 0:
            raise RailwayUnavailable(
                f"railway deployment list failed for {service}:"
                f" {first_line(err) or f'exit {code}'}",
                LOGIN_FIX,
            )
        try:
            raw = json_from(out) if out.strip() else []
        except ValueError:
            raise RailwayUnavailable(
                f"railway deployment list for {service} answered something that"
                " is not JSON",
                LOGIN_FIX,
            )
        if not isinstance(raw, list):
            raise RailwayUnavailable(
                f"railway deployment list for {service} answered no list", LOGIN_FIX
            )
        rows = [deployment_row(service, item) for item in raw if isinstance(item, dict)]
        # newest first whatever order the binary answers in — the watch judges
        # the first row
        rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
        return rows[:limit]

    def variables(
        self, service: str, fingerprint: Callable[[str, str], Optional[str]]
    ) -> dict[str, str]:
        """*service*'s production variables as ``{name: fingerprint(name,
        value)}``, each value reduced as it is read; a name *fingerprint*
        answers None for is left out. The values are production's credentials,
        so none is returned, and a refusal names the service and the exit code,
        never the binary's output."""
        code, out, _ = self._call(
            "variables",
            "--service",
            service,
            "--environment",
            ENVIRONMENT,
            "--json",
        )
        if code != 0:
            raise RailwayUnavailable(
                f"railway variables failed for {service} (exit {code})", LOGIN_FIX
            )
        try:
            raw = json_from(out)
        except ValueError:
            # refused below, after the handler (see RailwayUnavailable)
            raw = None
        if not isinstance(raw, dict):
            raise RailwayUnavailable(
                f"railway variables for {service} answered no JSON object", LOGIN_FIX
            )
        prints: dict[str, str] = {}
        for name, value in raw.items():
            if isinstance(value, str):
                digest = fingerprint(name, value)
                if digest is not None:
                    prints[name] = digest
        return prints
