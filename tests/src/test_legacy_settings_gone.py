"""The legacy settings, the worker switch and the config surfaces are retired
(plan ``2026-09-16-legacy-tear-out``, phase 02; #1222, #1205; fork F5).

Every `Settings` field the deleted legacy tier read — the three bare Telegram
variables it REQUIRED of every process, and sixteen more nothing reads —
is gone, so a process needs only the variables the target tier reads; no
field is required any more; the deployed entrypoints import with no
`TELEGRAM_*` variable in the environment; `WORKER_IMPL` and its contract
module are gone; `.env.example`, CI, the Makefile and the guides set no
variable nothing reads. The rule the deletion followed is measured here
rather than trusted: a field survives only if something outside the settings
module reads it.

The landing app (`landing/src/lib/telegram.ts`) reads `TELEGRAM_BOT_TOKEN` and
`ADMIN_TELEGRAM_CHAT_ID` from ITS OWN environment on Vercel — a different
consumer, untouched here; the retirement is the Python settings and the
Railway variables.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SETTINGS = ROOT / "src" / "config" / "settings.py"

#: The three the legacy tier required of every process (#1222).
REQUIRED_BY_THE_LEGACY_TIER = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHANNEL_ID",
    "ADMIN_TELEGRAM_CHAT_ID",
)

#: Every field measured at zero readers outside `src/config/settings.py` on
#: 2026-09-17 (`grep -rlE '\\bNAME\\b' src scripts storydump_cli`), including
#: the two read only by properties nothing called. `DB_SSLMODE` is read by
#: `database_url` (two callers) and stays.
RETIRED_FIELDS = REQUIRED_BY_THE_LEGACY_TIER + (
    "TELEGRAM_MAX_CONCURRENT_UPDATES",
    "TELEGRAM_RATE_LIMITER_ENABLED",
    "TELEGRAM_RATE_LIMITER_MAX_RETRIES",
    "MEDIA_DIR",
    "BACKUP_DIR",
    "BACKUP_RETENTION_DAYS",
    "FACEBOOK_APP_ID",
    "GOOGLE_REFRESH_TOKEN_TTL_DAYS",
    "CLOUD_STORAGE_PROVIDER",
    "CLOUD_UPLOAD_RETENTION_HOURS",
    "CLOUD_UPLOAD_TIMEOUT_SECONDS",
    "INSTAGRAM_PUBLISH_LIMIT_FALLBACK",
    "MEDIA_SYNC_INTERVAL_SECONDS",
    "ANTHROPIC_API_KEY",
    "CAPTION_MODEL",
    "META_GRAPH_API_VERSION",
)

#: The worker switch (#942) and its contract module.
WORKER_SWITCH = "WORKER_IMPL"

CODE_ROOTS = ("src", "scripts", "storydump_cli")
SENTINEL = "IMPORTED-WITHOUT-LEGACY-ENV="

#: Where a deploy or a developer sets variables: none may name a dead one.
SETTERS = (
    ".env.example",
    ".github/workflows/ci.yml",
    ".github/workflows/schema-drift.yml",
    "Makefile",
    "AGENTS.md",
    "README.md",
    "documentation/guides/testing-guide.md",
    "documentation/guides/dev-environment-setup.md",
    "documentation/guides/deployment.md",
    "documentation/guides/cloud-deployment.md",
    "documentation/guides/ci-cd-pipeline.md",
)


def _fields() -> dict:
    from src.config.settings import Settings

    return Settings.model_fields


def _code_text() -> str:
    return "\n".join(
        p.read_text(encoding="utf-8")
        for top in CODE_ROOTS
        for p in (ROOT / top).rglob("*.py")
        if p != SETTINGS
    )


def _settings_module_reads(name: str) -> bool:
    """A field read by a property in the settings module itself counts only
    if that property has a reader outside the module."""
    text = SETTINGS.read_text()
    body = text[text.index("class Settings") :]
    return len(re.findall(rf"self\.{name}\b", body)) > 0


def env_names_the_tree_reads() -> set[str]:
    """Every environment variable the code reads OUTSIDE `Settings`: the
    `environ.get("X")` / `env.get("X")` / `getenv("X")` / `environ["X"]`
    calls, the `*_VAR = "X"` / `*_ENV = "X"` constants the Telegram
    registration, the worker and the vocabulary read through, and every
    `TARGET_*` name spelled as a literal (the worker's lane knobs live in a
    dict of names)."""
    text = _code_text()
    names = set(re.findall(r"(?:environ|env)\.get\(\s*\"([A-Z][A-Z0-9_]*)\"", text))
    names |= set(re.findall(r"getenv\(\s*\"([A-Z][A-Z0-9_]*)\"", text))
    names |= set(re.findall(r"environ\[\s*\"([A-Z][A-Z0-9_]*)\"\s*\]", text))
    names |= set(re.findall(r"_(?:VAR|ENV)\s*=\s*\"([A-Z][A-Z0-9_]*)\"", text))
    names |= set(re.findall(r"\"(TARGET_[A-Z0-9_]+)\"", text))
    return names


# --- the fields ------------------------------------------------------------------


@pytest.mark.parametrize("name", RETIRED_FIELDS)
def test_the_retired_field_is_gone(name):
    assert name not in _fields(), f"{name} is still a Settings field"


def test_no_field_is_required_any_more():
    required = [n for n, f in _fields().items() if f.is_required()]
    assert required == [], (
        f"required fields: {required} — a process must not need a variable the"
        " target tier does not read"
    )


def test_every_surviving_field_has_a_reader():
    """The rule, measured: a field survives only if something reads it —
    directly from the tree, or through a settings property that is itself
    read outside the module."""
    text = _code_text()
    unread = []
    for name in _fields():
        if re.search(rf"\b{name}\b", text):
            continue
        if _settings_module_reads(name):
            continue
        unread.append(name)
    assert unread == [], f"fields nothing reads: {unread}"


def test_settings_construct_with_no_legacy_variable(monkeypatch):
    from src.config.settings import Settings

    for name in REQUIRED_BY_THE_LEGACY_TIER + (WORKER_SWITCH,):
        monkeypatch.delenv(name, raising=False)
    loaded = Settings(_env_file=None)
    assert loaded is not None


# --- the entrypoints and the switch ---------------------------------------------------


def test_the_deployed_entrypoints_import_with_no_telegram_variable():
    """A fresh interpreter, every `TELEGRAM_*` and the worker switch stripped
    from its environment: the three deployed entrypoints import. The
    `TARGET_*` family is read at run time, never at import."""
    env = {
        k: v
        for k, v in os.environ.items()
        if "TELEGRAM" not in k and k != WORKER_SWITCH
    }
    code = f"import src.main, src.api.app, src.worker\nprint({SENTINEL!r} + 'ok')\n"
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, env=env
    )
    assert proc.returncode == 0, proc.stderr[-1500:]
    assert f"{SENTINEL}ok" in proc.stdout.splitlines()


def test_the_worker_switch_and_its_contract_module_are_gone():
    assert not (ROOT / "src" / "worker_impl.py").exists()
    hits = [
        f"{p.relative_to(ROOT)}:{i}"
        for top in CODE_ROOTS
        for p in (ROOT / top).rglob("*.py")
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if WORKER_SWITCH in line
    ]
    assert hits == [], f"the worker switch is still named: {hits}"


def test_the_worker_refuses_to_boot_without_its_database_url(monkeypatch, capsys):
    """The settings-built fallback is gone: with the legacy loops deleted, a
    worker booted without `TARGET_DATABASE_URL` would otherwise run the
    target root against the legacy-configured `DB_*` database (the round-1
    lens of phase 01). It refuses with exit 2, naming the variable — the API
    already refused its way (`_engine_from_env`: no engine, 503 on every data
    route)."""
    import src.worker as worker

    monkeypatch.delenv("TARGET_DATABASE_URL", raising=False)
    with pytest.raises(SystemExit) as exc:
        worker.main()
    assert exc.value.code == 2
    assert "TARGET_DATABASE_URL" in capsys.readouterr().err


def test_create_engine_takes_no_settings_built_fallback():
    from src.services.target import unit_of_work

    with pytest.raises(ValueError, match="TARGET_DATABASE_URL"):
        unit_of_work.create_engine(None)


# --- the surfaces that set variables ---------------------------------------------------


def _assignments(text: str) -> set[str]:
    """`NAME=` at a line's start, commented out or not — a variable the file
    tells a reader to set."""
    return set(re.findall(r"^\s*#?\s*([A-Z][A-Z0-9_]*)=", text, re.M))


def test_env_example_names_only_variables_the_tree_reads():
    known = set(_fields()) | env_names_the_tree_reads()
    named = _assignments((ROOT / ".env.example").read_text())
    unknown = sorted(named - known)
    assert unknown == [], (
        f".env.example names variables nothing reads: {unknown} — a variable set"
        " for nothing is a lie the next reader has to disprove"
    )


#: The environment read outside `Settings`, measured on 2026-09-17 after the
#: deletion and pinned by EQUALITY (the plan's rule): a new run-time read is a
#: visible line in a diff, and a name that stops being read is one too.
ENV_READ_OUTSIDE_SETTINGS = {
    # the deployed roots' database and the runner's
    "TARGET_DATABASE_URL",
    "DATABASE_URL",
    # the one bot, its webhook and the harness's Telegram double
    "TARGET_TELEGRAM_BOT_TOKEN",
    "TARGET_TELEGRAM_BOT_USERNAME",
    "TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN",
    "TARGET_TELEGRAM_WEBHOOK_URL",
    "TARGET_TELEGRAM_WEBHOOK_AUTOREGISTER",
    "TARGET_TELEGRAM_WEBHOOK_MAX_CONNECTIONS",
    "TARGET_TELEGRAM_API_BASE",
    # the worker's knobs
    "TARGET_WORKER_INTERACTIVE_CONCURRENCY",
    "TARGET_WORKER_BULK_CONCURRENCY",
    "TARGET_USAGE_PRECHECK_ENABLED",
    "WORKER_LOG_LEVEL",
    # the API's process and the transit store
    "PORT",
    "WEB_CONCURRENCY",
    "CLOUDINARY_CLOUD_NAME",
    "CLOUDINARY_API_KEY",
    "CLOUDINARY_API_SECRET",
    "META_GRAPH_VERSION",
    # the notification sender and the deploy seam
    "RESEND_API_KEY",
    "EMAIL_FROM",
    "RAILWAY_API_TOKEN",
    "RAILWAY_PERSONAL_TOKEN",
    "RAILWAY_ENVIRONMENT_NAME",
    # the storydump CLI's own (a client; never a service's)
    "STORYDUMP_TOKEN",
    "STORYDUMP_API",
    "STORYDUMP_CONFIG_DIR",
    "STORYDUMP_INSECURE_HTTP",
}


def test_the_environment_read_outside_settings_is_pinned():
    assert env_names_the_tree_reads() == ENV_READ_OUTSIDE_SETTINGS, (
        "the set of environment variables read outside Settings changed —"
        " add the name here AND to .env.example, or drop it from both"
    )


def test_the_target_tiers_own_variables_are_documented():
    """The converse: what the deployed services need is in the example, so
    the file describes the tier that exists."""
    named = _assignments((ROOT / ".env.example").read_text())
    for name in (
        "TARGET_DATABASE_URL",
        "TARGET_TELEGRAM_BOT_TOKEN",
        "TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN",
        "TARGET_TELEGRAM_BOT_USERNAME",
        "DATABASE_URL",
        "ENCRYPTION_KEY",
    ):
        assert name in named, f"{name} is not in .env.example"


@pytest.mark.parametrize("path", SETTERS)
def test_no_setter_names_a_dead_variable(path):
    text = (ROOT / path).read_text()
    dead = [
        name
        for name in REQUIRED_BY_THE_LEGACY_TIER + (WORKER_SWITCH,)
        if re.search(rf"(^|[^A-Z_]){name}([^A-Z_]|$)", text, re.M)
    ]
    assert dead == [], f"{path} still names {dead}"


def test_the_batteries_recipes_set_no_dead_variable():
    """The RECIPE lines (`UNIT=` / `GATE=`), not the whole file: a battery
    may name a dead variable as the mutation it plants or the variable it
    unsets — what it may not do is set one for the tests to run under."""
    dead = re.compile(r"(^|[^A-Z_])(" + "|".join(REQUIRED_BY_THE_LEGACY_TIER) + r")=")
    hits = [
        f"{p.name}:{i}"
        for p in (ROOT / "tests" / "mutations").glob("*.sh")
        for i, line in enumerate(p.read_text().splitlines(), 1)
        if line.startswith(("UNIT=", "GATE=")) and dead.search(line)
    ]
    assert hits == [], f"batteries still setting a dead variable: {hits}"


def test_the_makefile_validates_the_settings_that_exist():
    text = (ROOT / "Makefile").read_text()
    assert "get_settings" not in text, (
        "validate-env calls a function that does not exist"
    )
    assert "from src.config.settings import settings" in text
    assert "'.[cli]'" in text or '".[cli]"' in text, (
        "install does not install the CLI extra"
    )
